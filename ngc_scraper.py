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
import random
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
MAX_WORKERS = 3                           # Eightfold rate-limits aggressively; keep low
REQUEST_TIMEOUT_SEC = 30
INTER_PAGE_DELAY_SEC  = 0.10              # pause between listing pages
DETAIL_JITTER_MIN_SEC = 0.25              # per-worker delay before each detail fetch
DETAIL_JITTER_MAX_SEC = 0.60

OUT_DIR = os.path.dirname(os.path.abspath(__file__))
NO_CSV         = os.path.join(OUT_DIR, "ngc_entry_level_no_clearance.csv")
UNKNOWN_CSV    = os.path.join(OUT_DIR, "ngc_entry_level_unknown_clearance.csv")
FORBIDDEN_CSV  = os.path.join(OUT_DIR, "ngc_entry_level_forbidden.csv")
CACHE_DIR      = os.path.join(OUT_DIR, ".ngc_detail_cache")  # per-job JSON, lets reruns resume

HEADERS_BASE = {
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (compatible; ngc-job-scraper/1.0)",
}

# NGC uses two parallel clearance boilerplates depending on the posting:
#
#   1. "CLEARANCE REQUIRED FOR START: Yes/No" — used on most US postings.
#      This is the authoritative signal when present.
#
#   2. "CLEARANCE TYPE: <value>" — used on Australian/UK postings and some
#      US SAP/contingent contracts. Used INSTEAD of the line above when
#      NGC wants to specify the type of clearance involved.
#
# Strategy: prefer (1) when present; fall back to (2). For (2), a non-empty
# clearance type (Secret, SC, SAP, AU-Protected, etc.) means clearance is
# involved → classify as "Yes". Only "None"/"N/A"/"Public Trust" etc. count
# as no-clearance.
CLEARANCE_REQUIRED_RE = re.compile(
    r"CLEARANCE\s+REQUIRED\s+FOR\s+START\s*[:\-]\s*(Yes|No)",
    re.IGNORECASE,
)
CLEARANCE_TYPE_RE = re.compile(
    r"CLEARANCE\s+TYPE\s*[:\-]\s*([^\n\r]+)",
    re.IGNORECASE,
)
# CLEARANCE TYPE values that mean "no actual security clearance required"
NO_CLEARANCE_TYPE_VALUES_RE = re.compile(
    r"^\s*(none|n/?a|not\s+required|not\s+applicable|public\s+trust)\s*$",
    re.IGNORECASE,
)

# ----------------------------------------------------------------------------
# HTTP helpers
# ----------------------------------------------------------------------------

def fetch_json(url, extra_headers=None, retries=6):
    """GET url, return parsed JSON. Handles 429 with Retry-After + backoff."""
    headers = dict(HEADERS_BASE)
    if extra_headers:
        headers.update(extra_headers)
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SEC) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 429:
                # Respect Retry-After if present; otherwise back off hard
                retry_after = e.headers.get("Retry-After", "")
                if retry_after.isdigit():
                    wait = int(retry_after) + random.uniform(0, 1)
                else:
                    wait = min(60.0, 5.0 * (2 ** attempt)) + random.uniform(0, 2)
                time.sleep(wait)
            elif e.code == 403:
                # Could be WAF/soft rate-limit OR a genuine "internal-only" posting.
                # Retry with backoff; if still 403 after retries, caller treats it
                # as Forbidden (separate bucket from network errors).
                time.sleep(min(30.0, 3.0 * (2 ** attempt)) + random.uniform(0, 1))
            elif 500 <= e.code < 600:
                time.sleep(1.0 * (2 ** attempt))
            else:
                # Non-retryable HTTP error (404, etc.) — fail fast
                raise RuntimeError(f"HTTP {e.code} for {url}: {e}")
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
            time.sleep(0.5 * (2 ** attempt))
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
    """Returns the position_details JSON. Caches successful responses to disk
    so reruns skip work and recover from rate-limit failures."""
    cache_path = os.path.join(CACHE_DIR, f"{pid}.json")
    if os.path.exists(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass  # corrupt cache entry; refetch

    url = (
        f"{BASE_URL}/api/pcsx/position_details"
        f"?position_id={pid}&domain={DOMAIN}&hl=en"
    )
    # This endpoint requires Referer + Origin matching the careers domain
    headers = {
        "Referer": f"{BASE_URL}/careers?pid={pid}",
        "Origin":  BASE_URL,
    }
    data = fetch_json(url, headers)

    # Write cache atomically (write to tmp, then rename) so a Ctrl-C mid-write
    # doesn't leave a half-baked file that breaks the next run.
    tmp_path = cache_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp_path, cache_path)
    return data


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
            # Skip jitter on cache hits — only throttle when we actually go to the network
            cache_path = os.path.join(CACHE_DIR, f"{p['id']}.json")
            if not os.path.exists(cache_path):
                time.sleep(random.uniform(DETAIL_JITTER_MIN_SEC, DETAIL_JITTER_MAX_SEC))
            d = fetch_position_details(p["id"])
            desc_html = d["data"].get("jobDescription", "") or ""
            desc_text = html_to_text(desc_html)
            return (p, extract_clearance(desc_text), None)
        except Exception as e:
            msg = str(e)
            # Distinguish persistent 403s (likely internal-only or withdrawn jobs)
            # from real errors (network, parsing, etc.)
            bucket = "Forbidden" if "HTTP Error 403" in msg or "HTTP 403" in msg else "Error"
            return (p, bucket, msg)

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

    counts = {"Yes": 0, "No": 0, "Unknown": 0, "Forbidden": 0, "Error": 0}
    for _, c, _ in results:
        counts[c] = counts.get(c, 0) + 1

    print(f"      clearance summary:")
    for k in ("No", "Yes", "Unknown", "Forbidden", "Error"):
        print(f"        {k:9s}: {counts.get(k, 0)}")

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

    # --- tertiary output: jobs the detail endpoint returned 403 for ---
    forbiddens = [(p, c) for p, c, _ in results if c == "Forbidden"]
    if forbiddens:
        with open(FORBIDDEN_CSV, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["req_id", "title", "department", "location", "public_url"])
            for p, _ in forbiddens:
                locs = p.get("standardizedLocations") or p.get("locations") or []
                w.writerow([
                    p.get("displayJobId", ""),
                    p.get("name", ""),
                    p.get("department", ""),
                    "; ".join(locs),
                    f"{BASE_URL}/careers/job/{p.get('id')}",
                ])
        print(f"      wrote {len(forbiddens)} jobs -> "
              f"{os.path.basename(FORBIDDEN_CSV)} (403 — likely internal-only or withdrawn)")

    errors = [(p, err) for p, c, err in results if c == "Error"]
    if errors:
        print(f"      {len(errors)} detail fetches failed with real errors; first 5:")
        for p, err in errors[:5]:
            print(f"        - {p.get('displayJobId')}: {err}")


# ----------------------------------------------------------------------------

def main():
    os.makedirs(CACHE_DIR, exist_ok=True)
    cached_at_start = len([f for f in os.listdir(CACHE_DIR) if f.endswith(".json")])
    if cached_at_start:
        print(f"(resume) {cached_at_start} job details already cached in "
              f"{os.path.basename(CACHE_DIR)}/ — will skip re-fetching those")

    start_time = time.time()
    listings = collect_all_listings()
    results  = fetch_clearance_verdicts(listings)
    write_outputs(results)

    cached_at_end = len([f for f in os.listdir(CACHE_DIR) if f.endswith(".json")])
    elapsed = time.time() - start_time
    print(f"\nDone in {elapsed:.1f}s "
          f"({cached_at_end - cached_at_start} new fetches, "
          f"{cached_at_end} total cached).")


if __name__ == "__main__":
    main()