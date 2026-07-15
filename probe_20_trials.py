"""
Quick one-off probe: pull a random sample of real trials straight from
clinicaltrials.gov's live search API and confirm every one of their NCT
IDs exists in the BigQuery mirror.

clinicaltrials.gov's v2 API has no "random sort" and no offset-based
pagination (only opaque pageToken cursors), so a true random draw across
the whole ~600k-study database isn't directly available. Instead this
pools results from several unrelated condition searches (spread across
different areas of medicine) and randomly samples from that combined pool,
which is a reasonable stand-in for "20 random trials" without needing to
paginate through the entire database.

Usage:
    python probe_20_trials.py
    python probe_20_trials.py --seed 42               # reproducible sample
    python probe_20_trials.py --condition diabetes    # sample only within one condition
"""

import argparse
import random

from google.cloud import bigquery

import app as webapp
from ctgov_to_bigquery import API_URL, build_session

NUM_TRIALS = 20
PER_TERM = 50
PER_TERM_SINGLE = 500  # used when --condition narrows the pool to one term

POOL_TERMS = [
    "diabetes", "cancer", "asthma", "depression", "hypertension",
    "arthritis", "obesity", "influenza", "alzheimer", "hiv",
    "stroke", "epilepsy", "migraine", "psoriasis", "insomnia",
    "anxiety", "osteoporosis", "hepatitis", "eczema", "anemia",
]


def fetch_pool(session, terms, per_term):
    """Runs one live search per term and pools every NCT ID returned,
    deduped, tagged with brief_title."""
    pool = {}
    for term in terms:
        params = {"format": "json", "pageSize": per_term, "query.cond": term}
        resp = session.get(API_URL, params=params, timeout=60)
        resp.raise_for_status()
        payload = resp.json()

        for study in payload.get("studies", []):
            ident = study.get("protocolSection", {}).get("identificationModule", {})
            nct_id = ident.get("nctId")
            if nct_id:
                pool[nct_id] = ident.get("briefTitle", "")
    return pool


def lookup_existing(client, nct_ids):
    query = f"""
        SELECT nct_id
        FROM {webapp.TABLE_REF}
        WHERE nct_id IN UNNEST(@ids)
    """
    job_config = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ArrayQueryParameter("ids", "STRING", sorted(nct_ids))
    ])
    rows = client.query(query, job_config=job_config).result()
    return {r["nct_id"] for r in rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seed", type=int, default=None, help="random seed, for a reproducible sample")
    parser.add_argument(
        "--condition", action="append", dest="conditions",
        help="repeatable; narrow the pool to specific condition search term(s) "
             "instead of the default diverse set",
    )
    args = parser.parse_args()
    rng = random.Random(args.seed)

    terms = args.conditions or POOL_TERMS
    per_term = PER_TERM_SINGLE if args.conditions else PER_TERM

    session = build_session()
    print(f"Pooling live trials from {len(terms)} condition search(es) on clinicaltrials.gov...\n")
    pool = fetch_pool(session, terms, per_term)
    print(f"Pool size: {len(pool)} unique trials\n")

    sample_ids = rng.sample(list(pool.keys()), min(NUM_TRIALS, len(pool)))
    print(f"Randomly sampled {len(sample_ids)} trial(s):\n")
    for nct_id in sample_ids:
        print(f"  {nct_id}: {pool[nct_id]}")

    client = bigquery.Client(project=webapp.PROJECT_ID)
    existing = lookup_existing(client, sample_ids)

    print()
    print("=" * 72)
    print(f"present in BigQuery: {len(existing)}/{len(sample_ids)}")
    print("=" * 72)
    print()

    missing = [nct_id for nct_id in sample_ids if nct_id not in existing]
    if missing:
        print("--- MISSING from BigQuery ---")
        for nct_id in missing:
            print(f"  {nct_id}: {pool[nct_id]}")
    else:
        print(f"All {len(sample_ids)} sampled trials were found in BigQuery.")


if __name__ == "__main__":
    main()
