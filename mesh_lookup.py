"""
mesh_lookup.py — look up keywords against NIH's MeSH database.

MeSH (Medical Subject Headings) is the NIH's controlled vocabulary of medical
terms. Every "descriptor" (the official heading, e.g. "Myocardial Infarction")
is broken down into one or more "concepts" (closely related meanings), and
each concept has a list of "terms" — alternate names / synonyms that a person
might actually type (e.g. "Heart Attack", "Cardiovascular Stroke").

This script takes one or more keywords, finds any matching MeSH descriptors
(matching either the official heading or one of its alternate names), and
prints/saves every concept and alternate name attached to each match.

No third-party libraries are required — only Python's built-in tools.

USAGE
-----
    python mesh_lookup.py diabetes "heart attack"
    python mesh_lookup.py --match exact "Diabetes Mellitus"
    python mesh_lookup.py --input keywords.txt --output results.json
    python mesh_lookup.py diabetes --csv results.csv

Run "python mesh_lookup.py --help" for all options.

HOW IT WORKS (for reference)
-----------------------------
NIH's MeSH linked-data API lives at https://id.nlm.nih.gov/mesh/ . It exposes:
  - /mesh/lookup/descriptor?label=...   search official heading names
  - /mesh/lookup/term?label=...         search alternate names directly
  - /mesh/<ID>.json                     fetch full details for any resource
  - /mesh/sparql                        run a SPARQL query (used here only to
                                         trace an alternate name back to the
                                         descriptor it belongs to)
This is a free, public, no-API-key-required NIH service.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

MESH_BASE = "https://id.nlm.nih.gov/mesh"
SPARQL_URL = f"{MESH_BASE}/sparql"
# Resource identifiers stored *inside* MeSH's triple store are always "http://",
# even though the API itself is served over https. SPARQL queries must use
# this form or triples simply won't match.
MESH_RESOURCE_BASE = "http://id.nlm.nih.gov/mesh"
USER_AGENT = "mesh-lookup-script/1.0 (contact: rohitx@hotmail.com)"


class MeshApiError(Exception):
    """Raised when the NIH MeSH API can't be reached or returns bad data."""


def _http_get_json(url: str, params: dict[str, str] | None = None) -> Any:
    """GET a URL (with optional query params) and parse the JSON response."""
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise MeshApiError(f"NIH MeSH API returned an error ({exc.code}) for: {url}") from exc
    except urllib.error.URLError as exc:
        raise MeshApiError(f"Could not reach the NIH MeSH API: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise MeshApiError(f"NIH MeSH API returned unreadable data for: {url}") from exc


def _resource_id(uri: str) -> str:
    """Turn a full resource URI like '.../mesh/D003920' into just 'D003920'."""
    return uri.rstrip("/").rsplit("/", 1)[-1]


class MeshClient:
    """Talks to the NIH MeSH API, with simple in-memory caching so the same
    concept/term is never fetched twice during one run."""

    def __init__(self, delay: float = 0.1):
        self.delay = delay
        self._cache: dict[str, Any] = {}

    def _get_json_cached(self, resource_id: str) -> Any:
        if resource_id not in self._cache:
            time.sleep(self.delay)
            self._cache[resource_id] = _http_get_json(f"{MESH_BASE}/{resource_id}.json")
        return self._cache[resource_id]

    def search_descriptors(self, keyword: str, match: str, limit: int) -> list[dict[str, str]]:
        """Search official MeSH heading names for a keyword."""
        time.sleep(self.delay)
        results = _http_get_json(
            f"{MESH_BASE}/lookup/descriptor",
            {"label": keyword, "match": match, "limit": str(limit)},
        )
        return [{"id": _resource_id(r["resource"]), "label": r["label"]} for r in results]

    def search_terms(self, keyword: str, match: str, limit: int) -> list[dict[str, str]]:
        """Search alternate names (entry terms) for a keyword."""
        time.sleep(self.delay)
        results = _http_get_json(
            f"{MESH_BASE}/lookup/term",
            {"label": keyword, "match": match, "limit": str(limit)},
        )
        return [{"id": _resource_id(r["resource"]), "label": r["label"]} for r in results]

    def descriptors_owning_term(self, term_id: str) -> list[str]:
        """Given an alternate-name term, find which descriptor(s) it belongs to."""
        time.sleep(self.delay)
        query = f"""
PREFIX meshv: <http://id.nlm.nih.gov/mesh/vocab#>
SELECT DISTINCT ?d WHERE {{
  ?concept meshv:term <{MESH_RESOURCE_BASE}/{term_id}> .
  {{ ?d meshv:preferredConcept ?concept }}
  UNION
  {{ ?d meshv:concept ?concept }}
}}
"""
        data = _http_get_json(SPARQL_URL, {"query": query, "format": "JSON"})
        return [_resource_id(b["d"]["value"]) for b in data["results"]["bindings"]]

    def expand_descriptor(self, descriptor_id: str) -> dict[str, Any] | None:
        """Fetch a descriptor and every concept + alternate name under it."""
        try:
            descriptor = self._get_json_cached(descriptor_id)
        except MeshApiError:
            return None

        concept_ids: list[str] = []
        if "preferredConcept" in descriptor:
            concept_ids.append(_resource_id(descriptor["preferredConcept"]))
        for c in _as_list(descriptor.get("concept")):
            cid = _resource_id(c)
            if cid not in concept_ids:
                concept_ids.append(cid)

        concepts = []
        for concept_id in concept_ids:
            try:
                concept = self._get_json_cached(concept_id)
            except MeshApiError:
                continue

            term_ids: list[str] = []
            if "preferredTerm" in concept:
                term_ids.append(_resource_id(concept["preferredTerm"]))
            for t in _as_list(concept.get("term")):
                tid = _resource_id(t)
                if tid not in term_ids:
                    term_ids.append(tid)

            preferred_term_id = _resource_id(concept["preferredTerm"]) if "preferredTerm" in concept else None
            alternate_names = []
            for term_id in term_ids:
                try:
                    term = self._get_json_cached(term_id)
                except MeshApiError:
                    continue
                label = term.get("prefLabel", {}).get("@value", "")
                if label and term_id != preferred_term_id:
                    alternate_names.append(label)

            concepts.append(
                {
                    "concept_id": concept_id,
                    "concept_label": concept.get("label", {}).get("@value", ""),
                    "alternate_names": alternate_names,
                }
            )

        return {
            "descriptor_id": descriptor_id,
            "descriptor_label": descriptor.get("label", {}).get("@value", ""),
            "concepts": concepts,
        }


def _as_list(value: Any) -> list[Any]:
    """The MeSH API returns a bare string for single values and a list for
    multiple values. Normalize both cases to a list."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def lookup_keyword(
    client: MeshClient,
    keyword: str,
    match: str = "contains",
    limit: int = 10,
    max_descriptors: int = 10,
) -> dict[str, Any]:
    """Find every MeSH descriptor matching a keyword (by heading or alternate
    name) and expand each into its full concept / alternate-name tree."""
    descriptor_hits = client.search_descriptors(keyword, match, limit)
    found_ids = {d["id"] for d in descriptor_hits}

    # Also search alternate names directly, then trace back to their descriptor.
    for term_hit in client.search_terms(keyword, match, limit):
        for descriptor_id in client.descriptors_owning_term(term_hit["id"]):
            if descriptor_id not in found_ids:
                found_ids.add(descriptor_id)
                descriptor_hits.append({"id": descriptor_id, "label": None})

    descriptors = []
    for hit in descriptor_hits[:max_descriptors]:
        expanded = client.expand_descriptor(hit["id"])
        if expanded:
            descriptors.append(expanded)

    return {"keyword": keyword, "descriptors": descriptors}


def print_human_readable(result: dict[str, Any]) -> None:
    keyword = result["keyword"]
    descriptors = result["descriptors"]
    print(f"\n=== '{keyword}' ===")
    if not descriptors:
        print("  No matching MeSH descriptors found.")
        return
    for d in descriptors:
        print(f"  Descriptor: {d['descriptor_label']} ({d['descriptor_id']})")
        for c in d["concepts"]:
            print(f"    Concept: {c['concept_label']} ({c['concept_id']})")
            if c["alternate_names"]:
                for name in c["alternate_names"]:
                    print(f"      - {name}")
            else:
                print("      (no alternate names on file)")


def write_csv(results: list[dict[str, Any]], path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["keyword", "descriptor_id", "descriptor_label", "concept_id", "concept_label", "alternate_name"])
        for result in results:
            for d in result["descriptors"]:
                for c in d["concepts"]:
                    if c["alternate_names"]:
                        for name in c["alternate_names"]:
                            writer.writerow([result["keyword"], d["descriptor_id"], d["descriptor_label"], c["concept_id"], c["concept_label"], name])
                    else:
                        writer.writerow([result["keyword"], d["descriptor_id"], d["descriptor_label"], c["concept_id"], c["concept_label"], ""])


def main() -> None:
    parser = argparse.ArgumentParser(description="Look up keywords against the NIH MeSH database.")
    parser.add_argument("keywords", nargs="*", help="Keywords to look up (e.g. diabetes \"heart attack\")")
    parser.add_argument("--input", help="Path to a text file with one keyword per line")
    parser.add_argument("--match", choices=["contains", "exact", "startswith"], default="contains", help="How to match keywords against MeSH names (default: contains)")
    parser.add_argument("--limit", type=int, default=10, help="Max search hits to consider per keyword (default: 10)")
    parser.add_argument("--max-descriptors", type=int, default=10, help="Max descriptors to expand per keyword (default: 10)")
    parser.add_argument("--delay", type=float, default=0.1, help="Seconds to wait between API calls, to be polite to NIH's servers (default: 0.1)")
    parser.add_argument("--output", help="Save full results as JSON to this path")
    parser.add_argument("--csv", help="Save a flat table of results as CSV to this path")
    parser.add_argument("--quiet", action="store_true", help="Don't print results to the screen")
    args = parser.parse_args()

    keywords = list(args.keywords)
    if args.input:
        with open(args.input, encoding="utf-8") as f:
            keywords.extend(line.strip() for line in f if line.strip())

    if not keywords:
        parser.error("Provide at least one keyword, or use --input to read them from a file.")

    client = MeshClient(delay=args.delay)
    results = []
    for keyword in keywords:
        try:
            result = lookup_keyword(client, keyword, match=args.match, limit=args.limit, max_descriptors=args.max_descriptors)
        except MeshApiError as exc:
            print(f"Error looking up '{keyword}': {exc}", file=sys.stderr)
            continue
        results.append(result)
        if not args.quiet:
            print_human_readable(result)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2, ensure_ascii=False)
        print(f"\nSaved full JSON results to {args.output}")

    if args.csv:
        write_csv(results, args.csv)
        print(f"Saved CSV results to {args.csv}")


if __name__ == "__main__":
    main()