"""
Local web front-end for querying the ClinicalTrials.gov BigQuery table
populated by ctgov_to_bigquery.py.

Setup:
    pip install flask google-cloud-bigquery

Auth: same as ctgov_to_bigquery.py (ADC or GOOGLE_APPLICATION_CREDENTIALS).

Usage:
    python app.py
    -> open http://127.0.0.1:5000
"""

import re
from datetime import datetime, date

from flask import Flask, render_template, request, jsonify
from google.cloud import bigquery

PROJECT_ID = "lily123"
DATASET_ID = "directdata"
TABLE_ID = "directtable"
TABLE_REF = f"`{PROJECT_ID}.{DATASET_ID}.{TABLE_ID}`"

MAX_PAGE_SIZE = 200
DEFAULT_PAGE_SIZE = 25
MAX_CUSTOM_SQL_ROWS = 500

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


def build_search_query(args):
    """Build the parameterized SEARCH query plus a human-readable rendering
    of it. Returns (query, params, display_sql, page, page_size); raises
    QueryBuildError on bad input."""
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
        where_clauses.append(
            "EXISTS (SELECT 1 FROM UNNEST(conditions) AS c WHERE LOWER(c) LIKE @condition)"
        )
        params.append(bigquery.ScalarQueryParameter("condition", "STRING", f"%{condition.lower()}%"))

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


if __name__ == "__main__":
    app.run(debug=True)
