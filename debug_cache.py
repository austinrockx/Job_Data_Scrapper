#!/usr/bin/env python3
"""
Targeted debug: call the scraper's own fetch_position_details() for one job
and verify whether it actually writes to the cache directory.

If this writes a file successfully, the scraper SHOULD be caching during its
real runs — and we'll need to look at why concurrent execution behaves
differently. If this FAILS to write, there's a bug in the function itself.
"""
import os, sys, time, traceback

# Import the scraper module from the same directory
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ngc_scraper

print(f"Module file:      {ngc_scraper.__file__}")
print(f"Module CACHE_DIR: {ngc_scraper.CACHE_DIR}")
print(f"Cache dir exists: {os.path.isdir(ngc_scraper.CACHE_DIR)}")

# Make sure cache dir exists (same call the scraper makes in main())
os.makedirs(ngc_scraper.CACHE_DIR, exist_ok=True)

# Wipe any stray test files from earlier debugging so we can see fresh state
for f in os.listdir(ngc_scraper.CACHE_DIR):
    if f.startswith("test_") or f.startswith("bulk_"):
        os.remove(os.path.join(ngc_scraper.CACHE_DIR, f))

print(f"Cache contents BEFORE call: {os.listdir(ngc_scraper.CACHE_DIR)}")

# Use a known-good position ID (from the first job in the first probe)
pid = 1340057843754
expected_cache_path = os.path.join(ngc_scraper.CACHE_DIR, f"{pid}.json")
print(f"\nExpected cache file: {expected_cache_path}")

print(f"\nCalling ngc_scraper.fetch_position_details({pid})...")
try:
    data = ngc_scraper.fetch_position_details(pid)
    print(f"  returned successfully, top-level keys: {list(data.keys())}")
except Exception as e:
    print(f"  FAILED: {type(e).__name__}: {e}")
    traceback.print_exc()
    sys.exit(1)

print(f"\nCache contents AFTER call: {os.listdir(ngc_scraper.CACHE_DIR)}")
print(f"Expected file exists:      {os.path.exists(expected_cache_path)}")
if os.path.exists(expected_cache_path):
    print(f"Expected file size:        {os.path.getsize(expected_cache_path)} bytes")

# Wait, look again
time.sleep(3)
print(f"\n3 seconds later:")
print(f"  expected file still exists: {os.path.exists(expected_cache_path)}")
print(f"  cache dir contents:         {os.listdir(ngc_scraper.CACHE_DIR)}")
