#!/usr/bin/env python3
"""
Diagnose why some jobs ended up in the 'Unknown' clearance bucket.

For each entry in ngc_entry_level_unknown_clearance.csv, find the cached
position_details JSON and print any clearance-related snippets from the
description. This is read-only — no network calls.

Run:  python3 inspect_unknowns.py
"""

import csv
import json
import os
import re
from html import unescape

OUT_DIR     = os.path.dirname(os.path.abspath(__file__))
UNKNOWN_CSV = os.path.join(OUT_DIR, "ngc_entry_level_unknown_clearance.csv")
CACHE_DIR   = os.path.join(OUT_DIR, ".ngc_detail_cache")

# Same loose regex the scraper uses
CLEARANCE_RE_STRICT = re.compile(
    r"CLEARANCE\s+REQUIRED\s+FOR\s+START\s*[:\-]\s*(Yes|No)", re.I
)
# Broader: any sentence mentioning clearance-related terms
CLEARANCE_TERMS = re.compile(
    r"clearance|secret|polygraph|TS/SCI|security access|background investigation",
    re.I,
)


def html_to_text(html):
    text = re.sub(r"<br\s*/?>", "\n", html, flags=re.I)
    text = re.sub(r"</p>", "\n\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def find_position_id(req_id):
    """Map an NGC requisition ID (R10xxxxxx) to its Eightfold position id by
    scanning the cache files. Slow but only ~50 lookups."""
    for fn in os.listdir(CACHE_DIR):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(CACHE_DIR, fn), "r", encoding="utf-8") as f:
                d = json.load(f)
            if d.get("data", {}).get("displayJobId") == req_id:
                return fn[:-5]  # strip .json
        except (json.JSONDecodeError, OSError):
            continue
    return None


def main():
    with open(UNKNOWN_CSV, "r", encoding="utf-8") as f:
        unknowns = list(csv.DictReader(f))
    print(f"Inspecting {len(unknowns)} 'Unknown' jobs from {os.path.basename(UNKNOWN_CSV)}\n")

    # Build a reqId -> position_id map in one pass (faster than per-job scan)
    reqid_to_pid = {}
    for fn in os.listdir(CACHE_DIR):
        if not fn.endswith(".json"): continue
        try:
            with open(os.path.join(CACHE_DIR, fn), "r", encoding="utf-8") as f:
                d = json.load(f)
            req = d.get("data", {}).get("displayJobId")
            if req:
                reqid_to_pid[req] = fn[:-5]
        except (json.JSONDecodeError, OSError):
            continue

    # Buckets so we can summarize
    no_terms_at_all     = []   # description never mentions clearance/secret/etc.
    has_terms_but_strict_missed = []   # mentions clearance but not in "CLEARANCE REQUIRED FOR START: X" format

    for i, row in enumerate(unknowns, 1):
        req_id = row["req_id"]
        pid = reqid_to_pid.get(req_id)
        if not pid:
            print(f"[{i:>2}] {req_id}  (NO CACHE) — {row['title']}")
            continue

        with open(os.path.join(CACHE_DIR, f"{pid}.json"), "r", encoding="utf-8") as f:
            d = json.load(f)
        desc_html = d.get("data", {}).get("jobDescription", "") or ""
        desc = html_to_text(desc_html)

        strict_hit = CLEARANCE_RE_STRICT.search(desc)
        term_hits  = CLEARANCE_TERMS.findall(desc)

        print(f"[{i:>2}] {req_id}  ({len(desc)} chars desc)  — {row['title']}")

        if strict_hit:
            # Regex matched here even though scraper said Unknown — shouldn't happen,
            # but worth flagging
            print(f"      !! strict regex DOES match here: {strict_hit.group(0)!r}")
        elif term_hits:
            has_terms_but_strict_missed.append(req_id)
            print(f"      clearance terms found ({len(term_hits)}). Sentences:")
            sents = re.findall(
                r"[^.\n]*\b(?:clearance|secret|polygraph|TS/SCI|security access|background investigation)\b[^.\n]*\.?",
                desc, re.I,
            )
            for s in sents[:3]:
                print(f"        • {s.strip()[:250]}")
        else:
            no_terms_at_all.append(req_id)
            # Show first 200 chars so we can eyeball whether the description is real
            print(f"      no clearance terms at all. Description preview:")
            print(f"        {desc[:200]!r}")
        print()

    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"Total unknowns inspected: {len(unknowns)}")
    print(f"  • No clearance language anywhere in description: {len(no_terms_at_all)}")
    print(f"  • Has clearance language but not in our regex format: "
          f"{len(has_terms_but_strict_missed)}")
    if has_terms_but_strict_missed:
        print(f"\n    Examples (first 5): {has_terms_but_strict_missed[:5]}")


if __name__ == "__main__":
    main()
