#!/usr/bin/env python3
"""
Northrop Grumman entry-level job scraper.

Scrapes jobs.northropgrumman.com (Eightfold AI platform) for all entry-level
job postings, fetches each detail page, and filters to those that don't
require security clearance to start.

NGC embeds a structured line in every posting:
    "CLEARANCE REQUIRED FOR START: Yes"  (or "No")
We use that as the authoritative signal.

Output:
    ngc_entry_level_no_clearance.csv  - jobs where the line says "No"
    ngc_entry_level_unknown_clearance.csv - jobs where the line is missing,
                                            for manual review

Stdlib-only; no pip install needed.
"""

import csv
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from html import unescape

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------

BASE_URL    = "https://jobs.northropgrumman.com"
DOMAIN      = "ngc.com"                   # Eightfold tenant identifier
PAGE_SIZE   = 10                          # Eightfold caps this tenant at 10/page
MAX_WORKERS = 8                           # parallel detail fetches
REQUEST_TIMEOUT_SEC = 30
INTER_PAGE_DELAY_SEC = 0.05               # polite pause between listing pages

OUT_DIR = os.path.dirname(os.path.abspath(__file__))
NO_CSV       = os.path.join(OUT_DIR, "ngc_entry_level_no_clearance.csv")
UNKNOWN_CSV  = os.path.join(OUT_DIR, "ngc_entry_level_unknown_clearance.csv")

HEADERS_BASE = {
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (compatible; ngc-job-scraper/1.0)",
}

# Tolerant regex: allows variable whitespace and ':' / '-' separators,
# case-insensitive. Captures the Yes/No verdict.
CLEARANCE_RE = re.compile(
    r"CLEARANCE\s+REQUIRED\s+FOR\s+START\s*[:\-]\s*(Yes|No)",
    re.IGNORECASE,
)

# ----------------------------------------------------------------------------
# HTTP helpers
# ----------------------------------------------------------------------------

def fetch_json(url, extra_headers=None, retries=3):
    headers = dict(HEADERS_BASE)
    if extra_headers:
        headers.update(extra_headers)
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SEC) as r:
                return json.load(r)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
            last_err = e
            time.sleep(0.5 * (2 ** attempt))   # 0.5s, 1s, 2s
    raise RuntimeError(f"fetch failed after {retries} attempts: {last_err}")


def fetch_search_page(start):
    params = {
        "domain": DOMAIN,
        "query": "",
        "location": "",
        "start": start,
        "sort_by": "match",
        "filter_experience_level": "Entry",
    }
    url = f"{BASE_URL}/api/pcsx/search?{urllib.parse.urlencode(params)}"
    return fetch_json(url)


def fetch_position_details(pid):
    url = (
        f"{BASE_URL}/api/pcsx/position_details"
        f"?position_id={pid}&domain={DOMAIN}&hl=en"
    )
    # This endpoint requires Referer + Origin matching the careers domain
    headers = {
        "Referer": f"{BASE_URL}/careers?pid={pid}",
        "Origin":  BASE_URL,
    }
    return fetch_json(url, headers)


# ----------------------------------------------------------------------------
# Parsing helpers
# ----------------------------------------------------------------------------

def html_to_text(html):
    """Crude but adequate HTML -> plain text for regex purposes."""
    text = re.sub(r"<br\s*/?>", "\n", html, flags=re.I)
    text = re.sub(r"</p>", "\n\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def extract_clearance(desc_text):
    """Returns 'Yes', 'No', or 'Unknown'."""
    m = CLEARANCE_RE.search(desc_text)
    if not m:
        return "Unknown"
    return m.group(1).capitalize()


def fmt_posted_date(ts):
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d")
    except (ValueError, OSError):
        return ""


# ----------------------------------------------------------------------------
# Pipeline stages
# ----------------------------------------------------------------------------

def collect_all_listings():
    """Paginate the search endpoint until all entry-level postings are gathered."""
    print("[1/3] Fetching listing pages...")
    first = fetch_search_page(0)
    total = first["data"]["count"]
    print(f"      total entry-level jobs: {total}")

    positions = list(first["data"]["positions"])
    num_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    print(f"      paginating {num_pages} pages of {PAGE_SIZE}...")

    for page_idx in range(1, num_pages):
        start = page_idx * PAGE_SIZE
        try:
            data = fetch_search_page(start)
            positions.extend(data["data"]["positions"])
        except Exception as e:
            print(f"      [WARN] page {page_idx} (start={start}) failed: {e}",
                  file=sys.stderr)
        if (page_idx + 1) % 10 == 0:
            print(f"      ... page {page_idx+1}/{num_pages} "
                  f"({len(positions)} positions collected)")
        time.sleep(INTER_PAGE_DELAY_SEC)

    # Deduplicate by Eightfold id (in case the list shifted mid-scrape)
    seen, deduped = set(), []
    for p in positions:
        if p["id"] not in seen:
            seen.add(p["id"])
            deduped.append(p)
    print(f"      collected {len(deduped)} unique positions "
          f"(server reported {total})")
    return deduped


def fetch_clearance_verdicts(positions):
    """For each position, fetch the detail page and classify clearance."""
    print(f"\n[2/3] Fetching details for {len(positions)} positions "
          f"(concurrency={MAX_WORKERS})...")

    def process(p):
        try:
            d = fetch_position_details(p["id"])
            desc_html = d["data"].get("jobDescription", "") or ""
            desc_text = html_to_text(desc_html)
            return (p, extract_clearance(desc_text), None)
        except Exception as e:
            return (p, "Error", str(e))

    results = []
    completed = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = [ex.submit(process, p) for p in positions]
        for fut in as_completed(futures):
            results.append(fut.result())
            completed += 1
            if completed % 25 == 0 or completed == len(positions):
                print(f"      ... {completed}/{len(positions)} processed")
    return results


def write_outputs(results):
    print(f"\n[3/3] Writing output CSVs...")

    counts = {"Yes": 0, "No": 0, "Unknown": 0, "Error": 0}
    for _, c, _ in results:
        counts[c] = counts.get(c, 0) + 1

    print(f"      clearance summary:")
    for k in ("No", "Yes", "Unknown", "Error"):
        print(f"        {k:8s}: {counts.get(k, 0)}")

    # --- main output: clearance == No ---
    keep = [(p, c) for p, c, _ in results if c == "No"]
    keep.sort(key=lambda x: x[0].get("postedTs") or 0, reverse=True)

    with open(NO_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "req_id", "title", "department", "location",
            "work_location_option", "posted_date", "url",
        ])
        for p, _ in keep:
            url = f"{BASE_URL}{p.get('positionUrl', '')}"
            locs = p.get("standardizedLocations") or p.get("locations") or []
            w.writerow([
                p.get("displayJobId", ""),
                p.get("name", ""),
                p.get("department", ""),
                "; ".join(locs),
                p.get("workLocationOption", ""),
                fmt_posted_date(p.get("postedTs")),
                url,
            ])
    print(f"      wrote {len(keep)} jobs -> {os.path.basename(NO_CSV)}")

    # --- secondary output: jobs where the clearance line was missing ---
    unknowns = [(p, c) for p, c, _ in results if c == "Unknown"]
    if unknowns:
        with open(UNKNOWN_CSV, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["req_id", "title", "department", "url"])
            for p, _ in unknowns:
                url = f"{BASE_URL}{p.get('positionUrl', '')}"
                w.writerow([
                    p.get("displayJobId", ""),
                    p.get("name", ""),
                    p.get("department", ""),
                    url,
                ])
        print(f"      wrote {len(unknowns)} jobs -> "
              f"{os.path.basename(UNKNOWN_CSV)} (manual review)")

    errors = [(p, err) for p, c, err in results if c == "Error"]
    if errors:
        print(f"      {len(errors)} detail fetches failed; first 5:")
        for p, err in errors[:5]:
            print(f"        - {p.get('displayJobId')}: {err}")


# ----------------------------------------------------------------------------

def main():
    start_time = time.time()
    listings = collect_all_listings()
    results  = fetch_clearance_verdicts(listings)
    write_outputs(results)
    elapsed = time.time() - start_time
    print(f"\nDone in {elapsed:.1f}s.")


if __name__ == "__main__":
    main()
