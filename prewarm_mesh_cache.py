"""
prewarm_mesh_cache.py -- fetch MeSH synonyms for known-common conditions
ahead of time, so the first user to search one of them doesn't pay the
live NIH API latency (see condition_synonyms.py / mesh_lookup.py).

Usage:
    python prewarm_mesh_cache.py diabetes acne cancer "thyroid cancer"
    python prewarm_mesh_cache.py --input conditions.txt
"""

import argparse

import condition_synonyms


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("conditions", nargs="*", help="Condition names to pre-warm")
    parser.add_argument("--input", help="Path to a text file with one condition per line")
    args = parser.parse_args()

    conditions = list(args.conditions)
    if args.input:
        with open(args.input, encoding="utf-8") as f:
            conditions.extend(line.strip() for line in f if line.strip())

    if not conditions:
        parser.error("Provide at least one condition, or use --input to read them from a file.")

    for condition in conditions:
        key = condition.strip().lower()
        if key in condition_synonyms._cache:
            print(f"'{condition}' -- already cached, skipping")
            continue
        print(f"'{condition}' -- fetching from NIH MeSH API...")
        terms = condition_synonyms.expand(condition)
        print(f"  -> {len(terms)} term(s): {', '.join(terms)}")


if __name__ == "__main__":
    main()
