"""
Diagnose why a filter combination (condition/status/sex/phase) returns a
different result set locally (BigQuery snapshot, via app.py's own query
builder) than the same search on clinicaltrials.gov's live API.

It does NOT re-implement the local query logic -- it imports
build_search_query() from app.py directly, strips the LIMIT/OFFSET, and
runs it unpaginated. That way this probe can never drift from what the web
app actually executes; any bug found here is a bug the app has too.

For the live side it calls the same /api/v2/studies endpoint
clinicaltrials.gov itself uses, translating sex/phase into Essie
`filter.advanced` expressions (AREA[Sex]..., AREA[Phase]...) and status into
`filter.overallStatus`. Condition goes through `query.cond`, which is a
full-text/MeSH search -- NOT a substring match -- so it can legitimately
return trials whose `conditions` field never contains the literal search
term. That gap is one of the things this probe is built to surface.

For every NCT ID clinicaltrials.gov matches that the local query didn't,
it classifies why:
  - ABSENT: not in the BigQuery table at all (never loaded, or the row was
    since added/changed upstream after the last ctgov_to_bigquery.py run)
  - STATUS_DRIFT: in the table, but overall_status disagrees with live
  - SEX_MISMATCH / PHASE_MISMATCH: in the table, but sex/phase disagrees
  - CONDITION_TEXT_GAP: in the table, all other fields agree, but the local
    substring match on `conditions` doesn't find the search term (the live
    API matched it via synonym/keyword/MeSH expansion instead)
  - UNEXPLAINED: none of the above -- needs a closer look

Usage:
    python probe_query_mismatch.py --condition diabetes --sex FEMALE \
        --phase PHASE2 --phase PHASE3 --status RECRUITING
"""

import argparse
import re
import sys
import time

import requests
from google.cloud import bigquery

import app as webapp
from ctgov_to_bigquery import API_URL, build_session

MAX_LIVE_RECORDS = 20000  # safety cap; a probe run isn't meant to re-mirror the whole DB


class ArgsShim:
    """Duck-types Flask's request.args (get/getlist) over a plain dict of
    lists, so build_search_query() can be called outside a request."""

    def __init__(self, values):
        self._values = values

    def get(self, key, default=None):
        vals = self._values.get(key)
        return vals[0] if vals else default

    def getlist(self, key):
        return self._values.get(key, [])


def build_local_match_query(condition, statuses, sexes, phases):
    """Reuses app.py's own WHERE-clause logic (condition/status/sex/phase
    only) but drops the LIMIT/OFFSET and adds `keywords`/`mesh_terms` to the
    SELECT list, so we get every matching row instead of one page."""
    args = ArgsShim({
        "condition": [condition] if condition else [],
        "status": statuses,
        "sex": sexes,
        "phase": phases,
    })
    query, params, display_sql, _page, _page_size = webapp.build_search_query(args)

    params = params[:-2]  # drop the page_size/offset params build_search_query appends last
    query = query.replace(", COUNT(*) OVER() AS total_count", "")
    query = query.replace(webapp.LIST_COLUMNS, webapp.LIST_COLUMNS + ", keywords, mesh_terms", 1)
    query = re.sub(r"LIMIT @page_size OFFSET @offset\s*$", "", query).strip()

    return query, params, display_sql


def fetch_local_matches(client, condition, statuses, sexes, phases):
    query, params, display_sql = build_local_match_query(condition, statuses, sexes, phases)
    print("--- Local (BigQuery) query, via app.py's build_search_query() ---")
    print(display_sql, "\n")

    job_config = bigquery.QueryJobConfig(query_parameters=params)
    rows = list(client.query(query, job_config=job_config).result())
    by_id = {}
    for r in rows:
        by_id[r["nct_id"]] = {
            "overall_status": r["overall_status"],
            "sex": r["sex"],
            "phases": list(r["phases"] or []),
            "conditions": list(r["conditions"] or []),
            "keywords": list(r["keywords"] or []),
            "mesh_terms": list(r["mesh_terms"] or []),
        }
    return by_id


def lookup_local_by_id(client, nct_ids):
    """Looks up NCT IDs regardless of filters, to tell 'never loaded' apart
    from 'loaded but excluded by a filter predicate'."""
    if not nct_ids:
        return {}
    query = f"""
        SELECT nct_id, overall_status, sex, phases, conditions, keywords, mesh_terms
        FROM {webapp.TABLE_REF}
        WHERE nct_id IN UNNEST(@ids)
    """
    job_config = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ArrayQueryParameter("ids", "STRING", sorted(nct_ids))
    ])
    rows = list(client.query(query, job_config=job_config).result())
    return {
        r["nct_id"]: {
            "overall_status": r["overall_status"],
            "sex": r["sex"],
            "phases": list(r["phases"] or []),
            "conditions": list(r["conditions"] or []),
            "keywords": list(r["keywords"] or []),
            "mesh_terms": list(r["mesh_terms"] or []),
        }
        for r in rows
    }


def build_essie_advanced_filter(sexes, phases):
    """Mirrors app.py's own sex-widening rule (selecting MALE/FEMALE also
    matches ALL-sex trials) so the live query models the same intent, not
    just a literal translation of the raw filter values.

    Values are quoted (AREA[Sex]"ALL", not AREA[Sex]ALL) because unquoted
    "ALL" is a reserved Essie wildcard that matches every document in the
    area regardless of its actual value -- confirmed by AREA[Sex]ALL alone
    returning the entire ~594k-study database. Quoting forces an exact-term
    match instead."""
    parts = []
    if sexes:
        widened = set(sexes)
        if widened & {"MALE", "FEMALE"}:
            widened.add("ALL")
        widened = sorted(widened)
        expr = " OR ".join(f'AREA[Sex]"{v}"' for v in widened)
        parts.append(f"({expr})" if len(widened) > 1 else expr)
    if phases:
        expr = " OR ".join(f'AREA[Phase]"{p}"' for p in phases)
        parts.append(f"({expr})" if len(phases) > 1 else expr)
    return " AND ".join(parts)


def extract_key_fields(study):
    protocol = study.get("protocolSection", {})
    ident = protocol.get("identificationModule", {})
    status = protocol.get("statusModule", {})
    design = protocol.get("designModule", {})
    conditions_mod = protocol.get("conditionsModule", {})
    eligibility = protocol.get("eligibilityModule", {})
    return {
        "nct_id": ident.get("nctId"),
        "overall_status": status.get("overallStatus"),
        "sex": eligibility.get("sex"),
        "phases": design.get("phases", []),
        "conditions": conditions_mod.get("conditions", []),
        "keywords": conditions_mod.get("keywords", []),
    }


def fetch_live_matches(condition, statuses, sexes, phases, max_records=MAX_LIVE_RECORDS):
    params = {"format": "json", "pageSize": 1000, "countTotal": "true"}
    if condition:
        params["query.cond"] = condition
    if statuses:
        params["filter.overallStatus"] = ",".join(statuses)
    advanced = build_essie_advanced_filter(sexes, phases)
    if advanced:
        params["filter.advanced"] = advanced

    print("--- Live (clinicaltrials.gov) query ---")
    print(f"GET {API_URL}")
    for k, v in params.items():
        print(f"  {k} = {v}")
    print()

    session = build_session()
    by_id = {}
    total_count = None
    page_token = None

    while True:
        req_params = dict(params)
        if page_token:
            req_params["pageToken"] = page_token
        resp = session.get(API_URL, params=req_params, timeout=60)
        resp.raise_for_status()
        payload = resp.json()

        if total_count is None:
            total_count = payload.get("totalCount")
            print(f"clinicaltrials.gov reports totalCount = {total_count}\n")
            if total_count and total_count > max_records:
                print(
                    f"WARNING: totalCount ({total_count}) exceeds --max-live-records "
                    f"({max_records}); the diff below is against a partial live sample. "
                    f"Narrow your filters or raise --max-live-records for a full diff.\n"
                )

        for study in payload.get("studies", []):
            fields = extract_key_fields(study)
            nct_id = fields.pop("nct_id")
            if nct_id:
                by_id[nct_id] = fields

        page_token = payload.get("nextPageToken")
        if not page_token or len(by_id) >= max_records:
            break
        time.sleep(0.2)  # be polite to the public API

    return by_id, total_count


def classify_missing(nct_id, live_fields, local_lookup, condition, statuses, sexes, phases):
    local = local_lookup.get(nct_id)
    if local is None:
        return "ABSENT", "not present in the BigQuery table at all"

    widened_sexes = set(sexes)
    if widened_sexes & {"MALE", "FEMALE"}:
        widened_sexes.add("ALL")

    if statuses and local["overall_status"] not in statuses:
        return "STATUS_DRIFT", (
            f"local overall_status={local['overall_status']!r} vs "
            f"live overall_status={live_fields['overall_status']!r}"
        )
    if widened_sexes and local["sex"] not in widened_sexes:
        return "SEX_MISMATCH", f"local sex={local['sex']!r} vs live sex={live_fields['sex']!r}"
    if phases and not (set(local["phases"]) & set(phases)):
        return "PHASE_MISMATCH", (
            f"local phases={local['phases']!r} vs live phases={live_fields['phases']!r}"
        )
    if condition:
        needle = condition.lower()
        local_hit = (
            any(needle in c.lower() for c in local["conditions"])
            or any(needle in m.lower() for m in local["mesh_terms"])
        )
        if not local_hit:
            return "CONDITION_TEXT_GAP", (
                f"local conditions={local['conditions']!r} and "
                f"mesh_terms={local['mesh_terms']!r} contain no literal "
                f"{condition!r}; live matched it via query.cond's synonym/keyword "
                f"expansion (live conditions={live_fields['conditions']!r}, "
                f"live keywords={live_fields['keywords']!r})"
            )

    return "UNEXPLAINED", f"local={local!r} live={live_fields!r}"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--condition", default="", help="e.g. diabetes")
    parser.add_argument("--status", action="append", default=[], help="repeatable, e.g. --status RECRUITING")
    parser.add_argument("--sex", action="append", default=[], help="repeatable, e.g. --sex FEMALE")
    parser.add_argument("--phase", action="append", default=[], help="repeatable, e.g. --phase PHASE2 --phase PHASE3")
    parser.add_argument("--max-live-records", type=int, default=MAX_LIVE_RECORDS)
    parser.add_argument("--show-ids", type=int, default=15, help="max example NCT IDs to print per category")
    args = parser.parse_args()

    if not any([args.condition, args.status, args.sex, args.phase]):
        parser.error("pass at least one of --condition/--status/--sex/--phase")

    client = bigquery.Client(project=webapp.PROJECT_ID)

    local_matches = fetch_local_matches(client, args.condition, args.status, args.sex, args.phase)
    print(f"Local matches: {len(local_matches)}\n")

    try:
        live_matches, live_total = fetch_live_matches(
            args.condition, args.status, args.sex, args.phase, args.max_live_records
        )
    except requests.HTTPError as e:
        print(f"clinicaltrials.gov API error: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"Live matches fetched: {len(live_matches)} (totalCount={live_total})\n")

    local_ids = set(local_matches)
    live_ids = set(live_matches)
    missing_from_local = live_ids - local_ids
    extra_in_local = local_ids - live_ids

    print("=" * 72)
    print(f"live-only (clinicaltrials.gov matched, local search missed): {len(missing_from_local)}")
    print(f"local-only (local search matched, clinicaltrials.gov didn't): {len(extra_in_local)}")
    print("=" * 72, "\n")

    if missing_from_local:
        local_lookup = lookup_local_by_id(client, missing_from_local)
        categories = {}
        for nct_id in sorted(missing_from_local):
            category, detail = classify_missing(
                nct_id, live_matches[nct_id], local_lookup, args.condition, set(args.status), args.sex, args.phase
            )
            categories.setdefault(category, []).append((nct_id, detail))

        print("--- Why clinicaltrials.gov results were missing locally ---\n")
        for category, items in sorted(categories.items(), key=lambda kv: -len(kv[1])):
            print(f"[{category}] {len(items)} trial(s)")
            for nct_id, detail in items[: args.show_ids]:
                print(f"    {nct_id}: {detail}")
            if len(items) > args.show_ids:
                print(f"    ... and {len(items) - args.show_ids} more")
            print()

    if extra_in_local:
        print("--- Trials the local search matched but clinicaltrials.gov didn't (sample) ---")
        for nct_id in sorted(extra_in_local)[: args.show_ids]:
            print(f"    {nct_id}: local={local_matches[nct_id]!r}")
        if len(extra_in_local) > args.show_ids:
            print(f"    ... and {len(extra_in_local) - args.show_ids} more")


if __name__ == "__main__":
    main()