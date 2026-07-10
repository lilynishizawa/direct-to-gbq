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
from datetime import datetime, timezone

import requests
from google.cloud import bigquery

# --- Config -----------------------------------------------------------------

PROJECT_ID = "lily123"
DATASET_ID = "directdata"
TABLE_ID = "directtable"

TOTAL_RECORDS = 10000
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
    bigquery.SchemaField("conditions", "STRING", mode="REPEATED"),
    bigquery.SchemaField("keywords", "STRING", mode="REPEATED"),
    bigquery.SchemaField("enrollment_count", "INTEGER"),
    bigquery.SchemaField("healthy_volunteers", "BOOLEAN"),
    bigquery.SchemaField("sex", "STRING"),
    bigquery.SchemaField("minimum_age_days", "INTEGER"),
    bigquery.SchemaField("brief_summary", "STRING"),
    bigquery.SchemaField("has_results", "BOOLEAN"),
    bigquery.SchemaField("retrieved_at", "TIMESTAMP"),
    bigquery.SchemaField("trial", "JSON", mode="REQUIRED"),
]


def fetch_studies(total_records=TOTAL_RECORDS, page_size=PAGE_SIZE):
    """Generator that yields raw study dicts from the ClinicalTrials.gov API,
    paginating with nextPageToken until total_records have been yielded."""
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

        resp = requests.get(API_URL, params=params, timeout=60)
        resp.raise_for_status()
        payload = resp.json()

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

    enrollment_count = design.get("enrollmentInfo", {}).get("count")

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
        "lead_sponsor": sponsor.get("leadSponsor", {}).get("name"),
        "conditions": conditions_mod.get("conditions", []),
        "keywords": conditions_mod.get("keywords", []),
        "enrollment_count": int(enrollment_count) if enrollment_count is not None else None,
        "healthy_volunteers": eligibility.get("healthyVolunteers"),
        "sex": eligibility.get("sex"),
        "minimum_age_days": parse_age_to_days(eligibility.get("minimumAge")),
        "brief_summary": description.get("briefSummary"),
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
