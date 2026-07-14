"""
Stream clinical trial records from ClinicalTrials.gov straight into BigQuery.

No intermediate file is ever written to disk: each page of results fetched
from the API is held in memory as newline-delimited JSON and handed directly
to BigQuery's load_table_from_file() using an io.BytesIO buffer.

Setup:
    pip install requests google-cloud-bigquery

Auth (pick one):
    - gcloud auth application-default login
    - or set GOOGLE_APPLICATION_CREDENTIALS to a service-account key path

Usage:
    python ctgov_to_bigquery.py
"""

import io
import json
import re
import sys
import time
from datetime import datetime, timezone

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from google.cloud import bigquery

# --- Config -----------------------------------------------------------------

PROJECT_ID = "lily123"
DATASET_ID = "directdata"
TABLE_ID = "directtable"

TOTAL_RECORDS = 100000
PAGE_SIZE = 1000  # ClinicalTrials.gov API v2 max page size

API_URL = "https://clinicaltrials.gov/api/v2/studies"

TABLE_SCHEMA = [
    bigquery.SchemaField("nct_id", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("brief_title", "STRING"),
    bigquery.SchemaField("official_title", "STRING"),
    bigquery.SchemaField("overall_status", "STRING"),
    bigquery.SchemaField("study_type", "STRING"),
    bigquery.SchemaField("phases", "STRING", mode="REPEATED"),
    bigquery.SchemaField("start_date", "STRING"),
    bigquery.SchemaField("primary_completion_date", "STRING"),
    bigquery.SchemaField("completion_date", "STRING"),
    bigquery.SchemaField("lead_sponsor", "STRING"),
    bigquery.SchemaField("lead_sponsor_class", "STRING"),
    bigquery.SchemaField("conditions", "STRING", mode="REPEATED"),
    bigquery.SchemaField("keywords", "STRING", mode="REPEATED"),
    bigquery.SchemaField("intervention_types", "STRING", mode="REPEATED"),
    bigquery.SchemaField("interventions", "RECORD", mode="REPEATED", fields=[
        bigquery.SchemaField("type", "STRING"),
        bigquery.SchemaField("name", "STRING"),
    ]),
    bigquery.SchemaField("enrollment_count", "INTEGER"),
    bigquery.SchemaField("healthy_volunteers", "BOOLEAN"),
    bigquery.SchemaField("sex", "STRING"),
    bigquery.SchemaField("minimum_age_days", "INTEGER"),
    bigquery.SchemaField("maximum_age_days", "INTEGER"),
    bigquery.SchemaField("brief_summary", "STRING"),
    bigquery.SchemaField("eligibility_criteria", "STRING"),
    bigquery.SchemaField("inclusion_criteria", "STRING"),
    bigquery.SchemaField("exclusion_criteria", "STRING"),
    bigquery.SchemaField("locations", "RECORD", mode="REPEATED", fields=[
        bigquery.SchemaField("facility", "STRING"),
        bigquery.SchemaField("city", "STRING"),
        bigquery.SchemaField("state", "STRING"),
        bigquery.SchemaField("country", "STRING"),
        bigquery.SchemaField("status", "STRING"),
    ]),
    bigquery.SchemaField("secondary_outcomes", "RECORD", mode="REPEATED", fields=[
        bigquery.SchemaField("measure", "STRING"),
        bigquery.SchemaField("time_frame", "STRING"),
    ]),
    bigquery.SchemaField("has_results", "BOOLEAN"),
    bigquery.SchemaField("retrieved_at", "TIMESTAMP"),
    bigquery.SchemaField("trial", "JSON", mode="REQUIRED"),
]


def build_session():
    """Session with HTTP-level retries for connect/read/status failures
    (handles transient blips like connection resets mid-response)."""
    session = requests.Session()
    retry = Retry(
        total=5,
        connect=5,
        read=5,
        status=5,
        backoff_factor=2,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


MAX_PAGE_ATTEMPTS = 5


def fetch_page(session, params):
    """Fetch a single page, retrying on transient errors that occur outside
    urllib3's own retry window (e.g. a stream cut off after headers arrive)."""
    for attempt in range(1, MAX_PAGE_ATTEMPTS + 1):
        try:
            resp = session.get(API_URL, params=params, timeout=60)
            resp.raise_for_status()
            return resp.json()
        except (requests.exceptions.ChunkedEncodingError,
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as e:
            if attempt == MAX_PAGE_ATTEMPTS:
                raise
            wait = 2 ** attempt
            print(f"  page fetch failed ({e!r}), retrying in {wait}s "
                  f"(attempt {attempt}/{MAX_PAGE_ATTEMPTS})...")
            time.sleep(wait)


def fetch_studies(total_records=TOTAL_RECORDS, page_size=PAGE_SIZE):
    """Generator that yields raw study dicts from the ClinicalTrials.gov API,
    paginating with nextPageToken until total_records have been yielded."""
    session = build_session()
    fetched = 0
    page_token = None

    while fetched < total_records:
        remaining = total_records - fetched
        params = {
            "format": "json",
            "pageSize": min(page_size, remaining),
        }
        if page_token:
            params["pageToken"] = page_token

        payload = fetch_page(session, params)

        studies = payload.get("studies", [])
        if not studies:
            break

        for study in studies:
            yield study
            fetched += 1
            if fetched >= total_records:
                break

        print(f"  fetched {fetched}/{total_records} studies...")

        page_token = payload.get("nextPageToken")
        if not page_token:
            break


_AGE_UNIT_TO_DAYS = {
    "day": 1,
    "days": 1,
    "week": 7,
    "weeks": 7,
    "month": 30,
    "months": 30,
    "year": 365,
    "years": 365,
}

_AGE_RE = re.compile(r"(\d+)\s*(day|days|week|weeks|month|months|year|years)", re.IGNORECASE)


def parse_age_to_days(age_str):
    """Convert an eligibility age string (e.g. "18 Years", "6 Months") into
    an integer number of days. Returns None for missing/unparseable values
    such as "N/A"."""
    if not age_str:
        print(f"Warning: age string is None or empty, returning -1 days")
        return -1
    match = _AGE_RE.match(age_str.strip())
    if not match:
        print(f"Warning: could not parse age string '{age_str}', returning -1 days")
        return -1
    value, unit = match.groups()
    return int(value) * _AGE_UNIT_TO_DAYS[unit.lower()]


_EXCLUSION_HEADER_RE = re.compile(r"\n\s*Exclusion Criteria:?\s*\n", re.IGNORECASE)
_INCLUSION_HEADER_RE = re.compile(r"^\s*Inclusion Criteria:?\s*\n", re.IGNORECASE)


def split_eligibility_criteria(criteria_text):
    """ClinicalTrials.gov stores inclusion/exclusion criteria as a single
    free-text field with "Inclusion Criteria:"/"Exclusion Criteria:"
    headers inside it, not as separate structured fields. This splits on
    those headers as a best-effort heuristic; source formatting varies, so
    treat the split as approximate rather than guaranteed-accurate."""
    if not criteria_text:
        return None, None
    match = _EXCLUSION_HEADER_RE.search(criteria_text)
    if not match:
        return _INCLUSION_HEADER_RE.sub("", criteria_text).strip(), None
    inclusion = _INCLUSION_HEADER_RE.sub("", criteria_text[:match.start()]).strip()
    exclusion = criteria_text[match.end():].strip()
    return inclusion, exclusion


def flatten(study, retrieved_at):
    """Pull out commonly-queried fields; keep the full record in `trial`."""
    protocol = study.get("protocolSection", {})
    ident = protocol.get("identificationModule", {})
    status = protocol.get("statusModule", {})
    design = protocol.get("designModule", {})
    sponsor = protocol.get("sponsorCollaboratorsModule", {})
    conditions_mod = protocol.get("conditionsModule", {})
    eligibility = protocol.get("eligibilityModule", {})
    description = protocol.get("descriptionModule", {})
    arms_interventions = protocol.get("armsInterventionsModule", {})
    contacts_locations = protocol.get("contactsLocationsModule", {})
    outcomes = protocol.get("outcomesModule", {})

    enrollment_count = design.get("enrollmentInfo", {}).get("count")
    lead_sponsor = sponsor.get("leadSponsor", {})

    interventions = arms_interventions.get("interventions", [])
    intervention_types = sorted({
        intervention["type"]
        for intervention in interventions
        if intervention.get("type")
    })

    eligibility_criteria = eligibility.get("eligibilityCriteria")
    inclusion_criteria, exclusion_criteria = split_eligibility_criteria(eligibility_criteria)

    return {
        "nct_id": ident.get("nctId"),
        "brief_title": ident.get("briefTitle"),
        "official_title": ident.get("officialTitle"),
        "overall_status": status.get("overallStatus"),
        "study_type": design.get("studyType"),
        "phases": design.get("phases", []),
        "start_date": status.get("startDateStruct", {}).get("date"),
        "primary_completion_date": status.get("primaryCompletionDateStruct", {}).get("date"),
        "completion_date": status.get("completionDateStruct", {}).get("date"),
        "lead_sponsor": lead_sponsor.get("name"),
        "lead_sponsor_class": lead_sponsor.get("class"),
        "conditions": conditions_mod.get("conditions", []),
        "keywords": conditions_mod.get("keywords", []),
        "intervention_types": intervention_types,
        "interventions": [
            {"type": i.get("type"), "name": i.get("name")}
            for i in interventions
        ],
        "enrollment_count": int(enrollment_count) if enrollment_count is not None else None,
        "healthy_volunteers": eligibility.get("healthyVolunteers"),
        "sex": eligibility.get("sex"),
        "minimum_age_days": parse_age_to_days(eligibility.get("minimumAge")),
        "maximum_age_days": parse_age_to_days(eligibility.get("maximumAge")),
        "brief_summary": description.get("briefSummary"),
        "eligibility_criteria": eligibility_criteria,
        "inclusion_criteria": inclusion_criteria,
        "exclusion_criteria": exclusion_criteria,
        "locations": [
            {
                "facility": loc.get("facility"),
                "city": loc.get("city"),
                "state": loc.get("state"),
                "country": loc.get("country"),
                "status": loc.get("status"),
            }
            for loc in contacts_locations.get("locations", [])
        ],
        "secondary_outcomes": [
            {"measure": o.get("measure"), "time_frame": o.get("timeFrame")}
            for o in outcomes.get("secondaryOutcomes", [])
        ],
        "has_results": study.get("hasResults", False),
        "retrieved_at": retrieved_at,
        "trial": study,
    }


def build_ndjson_buffer(rows):
    """Serialize rows to an in-memory NDJSON buffer (never touches disk)."""
    buf = io.BytesIO()
    for row in rows:
        buf.write((json.dumps(row) + "\n").encode("utf-8"))
    buf.seek(0)
    return buf


def ensure_dataset(client, dataset_id):
    dataset_ref = bigquery.DatasetReference(client.project, dataset_id)
    try:
        client.get_dataset(dataset_ref)
    except Exception:
        print(f"Dataset {dataset_id} not found, creating it...")
        client.create_dataset(bigquery.Dataset(dataset_ref))


def main():
    client = bigquery.Client(project=PROJECT_ID)
    ensure_dataset(client, DATASET_ID)

    table_ref = f"{PROJECT_ID}.{DATASET_ID}.{TABLE_ID}"
    retrieved_at = datetime.now(timezone.utc).isoformat()

    print(f"Fetching {TOTAL_RECORDS} studies from ClinicalTrials.gov...")
    rows = (flatten(s, retrieved_at) for s in fetch_studies())

    # Load in chunks so we never hold all 10,000 records' worth of buffers
    # at once and can stream page-by-page straight into BigQuery.
    CHUNK_SIZE = PAGE_SIZE
    chunk = []
    total_loaded = 0
    first_chunk = True

    for row in rows:
        chunk.append(row)
        if len(chunk) >= CHUNK_SIZE:
            total_loaded += load_chunk(client, table_ref, chunk, first_chunk)
            first_chunk = False
            chunk = []

    if chunk:
        total_loaded += load_chunk(client, table_ref, chunk, first_chunk)

    print(f"Done. Loaded {total_loaded} rows into {table_ref}.")


def load_chunk(client, table_ref, chunk, first_chunk):
    buffer = build_ndjson_buffer(chunk)

    job_config = bigquery.LoadJobConfig(
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
        schema=TABLE_SCHEMA,
        # Truncate on the very first chunk so re-runs don't duplicate data,
        # then append the rest of the chunks onto that same load.
        write_disposition=(
            bigquery.WriteDisposition.WRITE_TRUNCATE
            if first_chunk
            else bigquery.WriteDisposition.WRITE_APPEND
        ),
    )

    job = client.load_table_from_file(buffer, table_ref, job_config=job_config)
    job.result()  # wait for completion, raises on error

    print(f"  loaded chunk of {len(chunk)} rows into {table_ref}")
    return len(chunk)


if __name__ == "__main__":
    try:
        main()
    except requests.HTTPError as e:
        print(f"ClinicalTrials.gov API error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
