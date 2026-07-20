"""
backfill_mesh_terms.py -- one-time backfill of the `mesh_terms` column added
to the `directtable` BigQuery table.

ctgov_to_bigquery.py now extracts each trial's own MeSH tagging
(derivedSection.conditionBrowseModule.meshes) into a `mesh_terms` column,
but that only applies to rows loaded *after* the change. Existing rows
already have this data sitting in the `trial` JSON column -- every trial
ever fetched from clinicaltrials.gov includes it -- so instead of
re-running the full multi-million-record fetch against the live API, this
pulls mesh_terms out of the `trial` JSON BigQuery already has on disk.

This does two things:
  1. Adds the `mesh_terms` column to the table's schema, if it isn't there
     yet. This part is metadata-only, free, and safe to run any time
     (no-op if you've already re-run ctgov_to_bigquery.py since the schema
     change, or already ran this script before).
  2. Runs an UPDATE that fills mesh_terms for every row from its own
     `trial` JSON.

Step 2 scans the `trial` JSON column across every row, which is not free.
By default this script only prints a cost estimate (a BigQuery dry run) and
makes no data changes. Pass --execute once you're happy with the estimate
to actually run the backfill.

Usage:
    python backfill_mesh_terms.py              # dry run: show cost estimate only
    python backfill_mesh_terms.py --execute     # add the column (if needed) and backfill
"""

import argparse

from google.cloud import bigquery

from ctgov_to_bigquery import PROJECT_ID, DATASET_ID, TABLE_ID

TABLE_REF = f"{PROJECT_ID}.{DATASET_ID}.{TABLE_ID}"

UPDATE_QUERY = f"""
UPDATE `{TABLE_REF}`
SET mesh_terms = ARRAY(
  SELECT JSON_VALUE(m, '$.term')
  FROM UNNEST(JSON_EXTRACT_ARRAY(trial, '$.derivedSection.conditionBrowseModule.meshes')) AS m
)
WHERE TRUE
"""


def ensure_column(client):
    table = client.get_table(TABLE_REF)
    if any(f.name == "mesh_terms" for f in table.schema):
        print("`mesh_terms` column already exists on the table -- skipping schema change.")
        return
    table.schema = list(table.schema) + [
        bigquery.SchemaField("mesh_terms", "STRING", mode="REPEATED")
    ]
    client.update_table(table, ["schema"])
    print("Added `mesh_terms` column to the table schema.")


def estimate_cost(client):
    job_config = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False)
    job = client.query(UPDATE_QUERY, job_config=job_config)
    gb = job.total_bytes_processed / 1e9
    print(f"Dry run: this backfill would scan approximately {gb:.2f} GB.")
    print(
        "(BigQuery on-demand pricing is roughly $6.25/TB scanned as of writing; "
        "check your own project's pricing/discounts to convert this to a dollar figure.)"
    )


def run_backfill(client):
    print("Running backfill...")
    job = client.query(UPDATE_QUERY)
    job.result()
    print(f"Done. Backfilled mesh_terms for {job.num_dml_affected_rows} row(s).")


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--execute", action="store_true",
        help="Actually run the backfill (default: dry run cost estimate only)",
    )
    args = parser.parse_args()

    client = bigquery.Client(project=PROJECT_ID)

    ensure_column(client)  # safe/free metadata change, always applied
    estimate_cost(client)

    if not args.execute:
        print("\nDry run only -- no data was changed. Re-run with --execute to backfill.")
        return

    run_backfill(client)


if __name__ == "__main__":
    main()