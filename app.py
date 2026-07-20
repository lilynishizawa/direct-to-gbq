"""
Local web front-end for querying the ClinicalTrials.gov BigQuery table
populated by ctgov_to_bigquery.py.

Setup:
    pip install -r requirements.txt

Auth: same as ctgov_to_bigquery.py (ADC or GOOGLE_APPLICATION_CREDENTIALS).
Fuzzy match also calls the Claude API (ANTHROPIC_API_KEY or `ant auth login`).

Usage:
    python app.py
    -> open http://127.0.0.1:5000

    Set FLASK_DEBUG=1 to run with the Flask debugger/reloader for local dev.
    In production this app is served by gunicorn instead (see Dockerfile).
"""

import json
import os
import re
from datetime import datetime, date

import anthropic
from flask import Flask, render_template, request, jsonify
from google.cloud import bigquery

import condition_synonyms

PROJECT_ID = "lily123"
DATASET_ID = "directdata"
TABLE_ID = "directtable"
TABLE_REF = f"`{PROJECT_ID}.{DATASET_ID}.{TABLE_ID}`"

MAX_PAGE_SIZE = 200
DEFAULT_PAGE_SIZE = 25
MAX_CUSTOM_SQL_ROWS = 500

FUZZY_MATCH_MODEL = "claude-opus-4-8"
FUZZY_MATCH_MAX_TRIALS = 100
FUZZY_MATCH_MAX_TOKENS = 32000
# claude-opus-4-8 pricing, per million tokens.
FUZZY_MATCH_INPUT_PRICE_PER_MTOK = 5.00
FUZZY_MATCH_OUTPUT_PRICE_PER_MTOK = 25.00

FORBIDDEN_SQL_KEYWORDS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|MERGE|TRUNCATE|GRANT|REVOKE|CALL|EXPORT|LOAD)\b",
    re.IGNORECASE,
)

SORTABLE_COLUMNS = {
    "nct_id", "brief_title", "overall_status", "study_type", "start_date",
    "primary_completion_date", "lead_sponsor", "enrollment_count", "has_results",
}

LIST_COLUMNS = """
    nct_id, brief_title, overall_status, study_type, phases, start_date,
    primary_completion_date, lead_sponsor, lead_sponsor_class, conditions,
    enrollment_count, sex, has_results
"""

app = Flask(__name__)
client = bigquery.Client(project=PROJECT_ID)


@app.context_processor
def inject_asset_version():
    # Appended as a ?v= query string on static asset URLs so browsers fetch
    # a fresh copy whenever the file changes, instead of serving a stale
    # cached one indefinitely.
    def asset_version(filename):
        path = os.path.join(app.static_folder, filename)
        try:
            return int(os.path.getmtime(path))
        except OSError:
            return 0

    return {"asset_version": asset_version}


def json_safe(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def row_to_dict(row):
    return {k: json_safe(v) for k, v in row.items()}


class QueryBuildError(ValueError):
    pass


def parse_bool_flag(raw):
    if raw == "true":
        return True
    if raw == "false":
        return False
    return None


def sanitize_single_select(raw_sql):
    """Allow the user to edit the generated SQL and run their edited version,
    while still guarding against multiple statements or anything other than
    a read-only SELECT (no bound parameters here since the text itself may
    have been hand-edited)."""
    stripped = raw_sql.strip()
    if stripped.endswith(";"):
        stripped = stripped[:-1].rstrip()
    if ";" in stripped:
        raise QueryBuildError("Only a single statement is allowed")
    if not re.match(r"^(SELECT|WITH)\b", stripped, re.IGNORECASE):
        raise QueryBuildError("Only SELECT queries are allowed")
    if FORBIDDEN_SQL_KEYWORDS.search(stripped):
        raise QueryBuildError("Query contains a disallowed keyword")
    return stripped


def sql_literal(value, type_):
    """Render a query-parameter value as a SQL literal, for human-readable
    display only. The actual query sent to BigQuery always uses bound
    parameters (below), so this never affects execution."""
    if value is None:
        return "NULL"
    if type_ == "STRING":
        escaped = value.replace("\\", "\\\\").replace("'", "\\'")
        return f"'{escaped}'"
    if type_ == "BOOL":
        return "TRUE" if value else "FALSE"
    return str(value)


def render_display_sql(query, params):
    display = query
    for p in params:
        if isinstance(p, bigquery.ArrayQueryParameter):
            literal = "[" + ", ".join(sql_literal(v, p.array_type) for v in p.values) + "]"
        else:
            literal = sql_literal(p.value, p.type_)
        display = re.sub(rf"@{re.escape(p.name)}\b", literal, display)
    return display.strip()


def build_filter_clauses(args):
    """Build the WHERE clauses + bound params shared by the search endpoint
    and the fuzzy-match candidate lookup. Raises QueryBuildError on bad
    input."""
    where_clauses = []
    params = []

    keyword = args.get("keyword", "").strip()
    if keyword:
        where_clauses.append(
            "(LOWER(brief_title) LIKE @keyword OR LOWER(official_title) LIKE @keyword "
            "OR LOWER(brief_summary) LIKE @keyword)"
        )
        params.append(bigquery.ScalarQueryParameter("keyword", "STRING", f"%{keyword.lower()}%"))

    condition = args.get("condition", "").strip()
    if condition:
        # Expand to the typed text plus its MeSH synonyms (e.g. "heart attack"
        # -> also "Myocardial Infarction") so a trial tagged only with the
        # clinical name still matches a colloquial search, matching how
        # clinicaltrials.gov's own condition search behaves.
        condition_terms = condition_synonyms.expand(condition)
        like_patterns = [f"%{t.lower()}%" for t in condition_terms]
        where_clauses.append(
            "(EXISTS (SELECT 1 FROM UNNEST(conditions) AS c, UNNEST(@condition_terms) AS t WHERE LOWER(c) LIKE t) "
            "OR EXISTS (SELECT 1 FROM UNNEST(keywords) AS k, UNNEST(@condition_terms) AS t WHERE LOWER(k) LIKE t) "
            # mesh_terms is clinicaltrials.gov's own MeSH tagging for the trial,
            # which sometimes uses different wording than the trial's own
            # conditions/keywords text (e.g. "Diabetes Mellitus, Type 2" vs a
            # trial that only wrote "Type 2 Diabetes" in `conditions`).
            "OR EXISTS (SELECT 1 FROM UNNEST(mesh_terms) AS m, UNNEST(@condition_terms) AS t WHERE LOWER(m) LIKE t))"
        )
        params.append(bigquery.ArrayQueryParameter("condition_terms", "STRING", like_patterns))

    sponsor = args.get("sponsor", "").strip()
    if sponsor:
        where_clauses.append("LOWER(lead_sponsor) LIKE @sponsor")
        params.append(bigquery.ScalarQueryParameter("sponsor", "STRING", f"%{sponsor.lower()}%"))

    def multi_filter(field, param_name, values, scalar_when_single=False):
        values = [v for v in values if v]
        if not values:
            return
        if scalar_when_single and len(values) == 1:
            where_clauses.append(f"{field} = @{param_name}")
            params.append(bigquery.ScalarQueryParameter(param_name, "STRING", values[0]))
        else:
            where_clauses.append(f"{field} IN UNNEST(@{param_name})")
            params.append(bigquery.ArrayQueryParameter(param_name, "STRING", values))

    multi_filter("overall_status", "statuses", args.getlist("status"), scalar_when_single=True)
    multi_filter("study_type", "study_types", args.getlist("study_type"))
    multi_filter("lead_sponsor_class", "sponsor_classes", args.getlist("sponsor_class"))

    # A trial restricted to "ALL" sexes is eligible for male and female
    # participants alike, so selecting MALE or FEMALE should also match
    # ALL-sex trials; selecting ALL by itself should not pull in MALE/FEMALE.
    sex_selected = [v for v in args.getlist("sex") if v]
    if sex_selected:
        sexes = set(sex_selected)
        if sexes & {"MALE", "FEMALE"}:
            sexes.add("ALL")
        sexes = sorted(sexes)
        if len(sexes) == 1:
            where_clauses.append("sex = @sexes")
            params.append(bigquery.ScalarQueryParameter("sexes", "STRING", sexes[0]))
        else:
            where_clauses.append("sex IN UNNEST(@sexes)")
            params.append(bigquery.ArrayQueryParameter("sexes", "STRING", sexes))

    phases = [p for p in args.getlist("phase") if p]
    if phases:
        where_clauses.append("EXISTS (SELECT 1 FROM UNNEST(phases) AS p WHERE p IN UNNEST(@phases))")
        params.append(bigquery.ArrayQueryParameter("phases", "STRING", phases))

    age_raw = args.get("age", "").strip()
    if age_raw:
        try:
            age_years = float(age_raw)
        except ValueError:
            raise QueryBuildError("age must be a number")
        if age_years < 0 or age_years > 130:
            raise QueryBuildError("age must be between 0 and 130")
        age_days = round(age_years * 365)
        # minimum/maximum_age_days is NULL (not stated) or -1 (unparseable
        # source string) when the trial's eligibility page didn't give a
        # usable bound -- treat both as "no restriction" rather than
        # excluding the trial.
        where_clauses.append(
            "(minimum_age_days IS NULL OR minimum_age_days < 0 OR minimum_age_days <= @age_days) "
            "AND (maximum_age_days IS NULL OR maximum_age_days < 0 OR maximum_age_days >= @age_days)"
        )
        params.append(bigquery.ScalarQueryParameter("age_days", "INT64", age_days))

    healthy_volunteers = parse_bool_flag(args.get("healthy_volunteers"))
    if healthy_volunteers is not None:
        where_clauses.append("healthy_volunteers = @healthy_volunteers")
        params.append(bigquery.ScalarQueryParameter("healthy_volunteers", "BOOL", healthy_volunteers))

    has_results = parse_bool_flag(args.get("has_results"))
    if has_results is not None:
        where_clauses.append("has_results = @has_results")
        params.append(bigquery.ScalarQueryParameter("has_results", "BOOL", has_results))

    for field, param_name, op in [
        ("enrollment_min", "enrollment_min", ">="),
        ("enrollment_max", "enrollment_max", "<="),
    ]:
        raw = args.get(field)
        if raw:
            try:
                value = int(raw)
            except ValueError:
                raise QueryBuildError(f"{field} must be an integer")
            where_clauses.append(f"enrollment_count {op} @{param_name}")
            params.append(bigquery.ScalarQueryParameter(param_name, "INT64", value))

    for field, param_name, op in [
        ("start_year_min", "start_year_min", ">="),
        ("start_year_max", "start_year_max", "<="),
    ]:
        raw = args.get(field)
        if raw:
            try:
                value = int(raw)
            except ValueError:
                raise QueryBuildError(f"{field} must be an integer")
            where_clauses.append(f"SAFE_CAST(SUBSTR(start_date, 1, 4) AS INT64) {op} @{param_name}")
            params.append(bigquery.ScalarQueryParameter(param_name, "INT64", value))

    return where_clauses, params


def build_search_query(args):
    """Build the parameterized SEARCH query plus a human-readable rendering
    of it. Returns (query, params, display_sql, page, page_size); raises
    QueryBuildError on bad input."""
    where_clauses, params = build_filter_clauses(args)

    sort = args.get("sort", "nct_id")
    if sort not in SORTABLE_COLUMNS:
        sort = "nct_id"
    direction = "DESC" if args.get("dir") == "desc" else "ASC"

    try:
        page = max(int(args.get("page", 1)), 1)
    except ValueError:
        page = 1
    try:
        page_size = min(max(int(args.get("page_size", DEFAULT_PAGE_SIZE)), 1), MAX_PAGE_SIZE)
    except ValueError:
        page_size = DEFAULT_PAGE_SIZE

    offset = (page - 1) * page_size
    where_sql = " AND ".join(where_clauses) if where_clauses else "TRUE"

    query = f"""
        SELECT {LIST_COLUMNS}, COUNT(*) OVER() AS total_count
        FROM {TABLE_REF}
        WHERE {where_sql}
        ORDER BY {sort} {direction}
        LIMIT @page_size OFFSET @offset
    """
    params.append(bigquery.ScalarQueryParameter("page_size", "INT64", page_size))
    params.append(bigquery.ScalarQueryParameter("offset", "INT64", offset))

    display_sql = render_display_sql(query, params)
    return query, params, display_sql, page, page_size


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/search/sql")
def api_search_sql():
    """Returns the SQL a search would run, without executing it against
    BigQuery -- lets the front end show the user the query for approval
    before any bytes get scanned."""
    try:
        _query, _params, display_sql, _page, _page_size = build_search_query(request.args)
    except QueryBuildError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"sql": display_sql})


@app.route("/api/search")
def api_search():
    try:
        query, params, _display_sql, page, page_size = build_search_query(request.args)
    except QueryBuildError as e:
        return jsonify({"error": str(e)}), 400

    job_config = bigquery.QueryJobConfig(query_parameters=params)
    try:
        result = list(client.query(query, job_config=job_config).result())
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    rows = [row_to_dict(r) for r in result]
    total = rows[0].pop("total_count") if rows else 0
    for r in rows:
        r.pop("total_count", None)

    return jsonify({"rows": rows, "total": total, "page": page, "page_size": page_size})


@app.route("/api/execute-sql", methods=["POST"])
def api_execute_sql():
    """Runs a user-supplied (possibly hand-edited) SQL query verbatim.
    Since the text may no longer match anything build_search_query produced,
    results are returned with whatever columns the query itself selects,
    rather than the fixed search-result schema."""
    body = request.get_json(silent=True) or {}
    raw_sql = (body.get("sql") or "").strip()
    if not raw_sql:
        return jsonify({"error": "No SQL provided"}), 400

    try:
        sql = sanitize_single_select(raw_sql)
    except QueryBuildError as e:
        return jsonify({"error": str(e)}), 400

    try:
        result = client.query(sql).result(max_results=MAX_CUSTOM_SQL_ROWS)
        columns = [field.name for field in result.schema]
        rows = [row_to_dict(r) for r in result]
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    return jsonify({"columns": columns, "rows": rows})


@app.route("/api/trial/<nct_id>")
def api_trial(nct_id):
    query = f"""
        SELECT
          nct_id, brief_title, official_title, overall_status, study_type, phases,
          start_date, primary_completion_date, completion_date, lead_sponsor,
          lead_sponsor_class, conditions, keywords, intervention_types, interventions,
          enrollment_count, healthy_volunteers, sex, minimum_age_days, maximum_age_days,
          brief_summary, inclusion_criteria, exclusion_criteria, locations,
          secondary_outcomes, has_results, retrieved_at
        FROM {TABLE_REF}
        WHERE nct_id = @nct_id
        LIMIT 1
    """
    job_config = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("nct_id", "STRING", nct_id)
    ])
    result = list(client.query(query, job_config=job_config).result())
    if not result:
        return jsonify({"error": "not found"}), 404
    return jsonify(row_to_dict(result[0]))


FUZZY_TRIAL_COLUMNS = """
    nct_id, brief_title, sex, minimum_age_days, maximum_age_days,
    brief_summary, inclusion_criteria, exclusion_criteria
"""

FUZZY_MATCH_INSTRUCTIONS = """You are a clinical trials reader.

Using only the patient information and each trial's inclusion/exclusion criteria given below, determine the patient's eligibility for every listed clinical trial.

For each trial, decide an overall eligibility of "Eligible", "Ineligible", or "Uncertain" -- use "Uncertain" when the criteria don't give you enough information to decide confidently either way. Then list out, as short bullet-style statements:
- which specific criteria make the patient ineligible
- which specific criteria you are uncertain about
- which specific criteria make the patient eligible

Base your judgment only on the information provided below. Do not assume anything about the patient that was not stated."""


def parse_patient_json(body):
    """Parse and validate the patient profile from a JSON request body.
    Raises QueryBuildError on bad input."""
    patient = body.get("patient") or {}

    sex = str(patient.get("sex", "")).strip().upper()
    if sex not in ("MALE", "FEMALE"):
        raise QueryBuildError("patient.sex must be MALE or FEMALE")

    try:
        age = float(patient.get("age"))
    except (TypeError, ValueError):
        raise QueryBuildError("patient.age must be a number")
    if age < 0 or age > 130:
        raise QueryBuildError("patient.age must be between 0 and 130")

    notes = str(patient.get("notes") or "").strip()
    return {"sex": sex, "age": age, "notes": notes}


def fetch_trials_by_ids(nct_ids):
    """Fetch the fuzzy-match trial columns for a specific list of NCT IDs,
    preserving the input order."""
    if not nct_ids:
        return []
    query = f"SELECT {FUZZY_TRIAL_COLUMNS} FROM {TABLE_REF} WHERE nct_id IN UNNEST(@nct_ids)"
    job_config = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ArrayQueryParameter("nct_ids", "STRING", nct_ids)
    ])
    rows_by_id = {r["nct_id"]: row_to_dict(r) for r in client.query(query, job_config=job_config).result()}
    return [rows_by_id[n] for n in nct_ids if n in rows_by_id]


def format_patient_block(patient):
    lines = [f"Age: {patient['age']} years", f"Sex: {patient['sex']}"]
    if patient.get("notes"):
        lines.append(f"Additional information: {patient['notes']}")
    return "\n".join(lines)


def format_trial_block(trial):
    return (
        f"NCT ID: {trial['nct_id']}\n"
        f"Title: {trial.get('brief_title') or ''}\n"
        f"Sex requirement: {trial.get('sex') or 'ALL'}\n"
        f"Minimum age (days): {trial.get('minimum_age_days')}\n"
        f"Maximum age (days): {trial.get('maximum_age_days')}\n"
        f"Brief summary: {trial.get('brief_summary') or ''}\n"
        f"Inclusion criteria: {trial.get('inclusion_criteria') or ''}\n"
        f"Exclusion criteria: {trial.get('exclusion_criteria') or ''}"
    )


def build_fuzzy_prompt(patient, trials):
    trial_blocks = "\n\n".join(format_trial_block(t) for t in trials)
    return (
        f"{FUZZY_MATCH_INSTRUCTIONS}\n\n"
        f"Patient information:\n{format_patient_block(patient)}\n\n"
        f"Trials:\n\n{trial_blocks}"
    )


def build_fuzzy_result_schema(nct_ids):
    return {
        "type": "object",
        "properties": {
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "nct_id": {"type": "string", "enum": nct_ids},
                        "overall": {"type": "string", "enum": ["Eligible", "Ineligible", "Uncertain"]},
                        "ineligible_criteria": {"type": "array", "items": {"type": "string"}},
                        "uncertain_criteria": {"type": "array", "items": {"type": "string"}},
                        "eligible_criteria": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": [
                        "nct_id", "overall", "ineligible_criteria",
                        "uncertain_criteria", "eligible_criteria",
                    ],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["results"],
        "additionalProperties": False,
    }


def estimate_fuzzy_match_cost(prompt):
    """Estimates the cost of running the fuzzy match prompt. Input cost is
    exact (via the token-counting endpoint); output cost can only be bounded
    by the max_tokens ceiling since actual output length depends on how much
    the model reasons and writes. Returns None if the estimate call fails --
    the prompt preview should still work even if this does not."""
    try:
        ai_client = anthropic.Anthropic()
        count = ai_client.messages.count_tokens(
            model=FUZZY_MATCH_MODEL,
            messages=[{"role": "user", "content": prompt}],
        )
        input_tokens = count.input_tokens
    except Exception:
        return None

    input_cost = input_tokens * FUZZY_MATCH_INPUT_PRICE_PER_MTOK / 1_000_000
    max_output_cost = FUZZY_MATCH_MAX_TOKENS * FUZZY_MATCH_OUTPUT_PRICE_PER_MTOK / 1_000_000
    return {
        "input_tokens": input_tokens,
        "input_cost": input_cost,
        "max_output_cost": max_output_cost,
        "max_total_cost": input_cost + max_output_cost,
    }


@app.route("/api/fuzzy-match/candidates")
def api_fuzzy_match_candidates():
    """The current search filters *are* the hard criteria -- whatever the
    main search returns (sex, status, age once that filter exists, etc.) is
    the candidate pool for AI matching. Returns the matching total, plus the
    trial list itself when it's small enough to feed to the AI step."""
    try:
        where_clauses, params = build_filter_clauses(request.args)
    except QueryBuildError as e:
        return jsonify({"error": str(e)}), 400

    where_sql = " AND ".join(where_clauses) if where_clauses else "TRUE"

    try:
        count_query = f"SELECT COUNT(*) AS total FROM {TABLE_REF} WHERE {where_sql}"
        job_config = bigquery.QueryJobConfig(query_parameters=params)
        total = list(client.query(count_query, job_config=job_config).result())[0]["total"]

        if total > FUZZY_MATCH_MAX_TRIALS:
            return jsonify({"total": total, "trials": None})

        detail_query = f"""
            SELECT nct_id, brief_title
            FROM {TABLE_REF}
            WHERE {where_sql}
            ORDER BY nct_id
            LIMIT {FUZZY_MATCH_MAX_TRIALS}
        """
        job_config = bigquery.QueryJobConfig(query_parameters=params)
        rows = [row_to_dict(r) for r in client.query(detail_query, job_config=job_config).result()]
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    return jsonify({"total": total, "trials": rows})


@app.route("/api/fuzzy-match/prompt", methods=["POST"])
def api_fuzzy_match_prompt():
    """Returns the exact prompt text that /api/fuzzy-match/run would send to
    Claude, so the user can review it before anything runs."""
    body = request.get_json(silent=True) or {}
    try:
        patient = parse_patient_json(body)
    except QueryBuildError as e:
        return jsonify({"error": str(e)}), 400

    nct_ids = body.get("nct_ids") or []
    if not nct_ids:
        return jsonify({"error": "No trials provided"}), 400
    if len(nct_ids) > FUZZY_MATCH_MAX_TRIALS:
        return jsonify({"error": f"Too many trials (max {FUZZY_MATCH_MAX_TRIALS})"}), 400

    try:
        trials = fetch_trials_by_ids(nct_ids)
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    prompt = build_fuzzy_prompt(patient, trials)
    return jsonify({"prompt": prompt, "cost": estimate_fuzzy_match_cost(prompt)})


@app.route("/api/fuzzy-match/run", methods=["POST"])
def api_fuzzy_match_run():
    """Runs the AI eligibility check over the given trials. Refuses to run
    when there are more than FUZZY_MATCH_MAX_TRIALS -- the front end should
    already be blocking that, but this is the real gate."""
    body = request.get_json(silent=True) or {}
    try:
        patient = parse_patient_json(body)
    except QueryBuildError as e:
        return jsonify({"error": str(e)}), 400

    nct_ids = body.get("nct_ids") or []
    if not nct_ids:
        return jsonify({"error": "No trials provided"}), 400
    if len(nct_ids) > FUZZY_MATCH_MAX_TRIALS:
        return jsonify({"error": f"Too many trials to run AI matching on (max {FUZZY_MATCH_MAX_TRIALS})"}), 400

    try:
        trials = fetch_trials_by_ids(nct_ids)
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    prompt = build_fuzzy_prompt(patient, trials)
    schema = build_fuzzy_result_schema([t["nct_id"] for t in trials])

    try:
        ai_client = anthropic.Anthropic()
        with ai_client.messages.stream(
            model=FUZZY_MATCH_MODEL,
            max_tokens=FUZZY_MATCH_MAX_TOKENS,
            thinking={"type": "adaptive"},
            output_config={"effort": "high", "format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            response = stream.get_final_message()
    except anthropic.RateLimitError:
        return jsonify({"error": "Rate limited by the AI provider. Please wait and try again."}), 429
    except anthropic.APIStatusError as e:
        return jsonify({"error": f"AI request failed: {e.message}"}), 502
    except anthropic.APIConnectionError as e:
        return jsonify({"error": f"Could not reach the AI provider: {e}"}), 502
    except Exception as e:
        return jsonify({"error": f"AI request failed: {e}"}), 502

    if response.stop_reason == "refusal":
        return jsonify({"error": "The AI declined to process this request."}), 502

    text = next((b.text for b in response.content if b.type == "text"), None)
    if not text:
        return jsonify({"error": "AI returned no output"}), 502
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return jsonify({"error": "AI returned invalid JSON"}), 502

    return jsonify({"results": parsed.get("results", [])})


if __name__ == "__main__":
    # In production this file is served by gunicorn (see Dockerfile), not this
    # block. This is only for local development: `python app.py`.
    debug = os.environ.get("FLASK_DEBUG") == "1"
    app.run(host="127.0.0.1" if debug else "0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=debug)
