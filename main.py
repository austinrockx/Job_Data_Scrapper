# northrop_entry_scraper.py

import time
import pandas as pd
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

"""
Northrop Grumman Entry-Level Job Scraper

This scraper:
1. Opens the Northrop Grumman job search page with your filters:
   - United States
   - Include eligible remote jobs
   - Experience level = Entry
   - Sort by profile match

2. Collects job IDs from each search results page.

3. Opens each job's detail page.

4. Checks whether the job description contains:
   "CLEARANCE REQUIRED FOR START: No"

5. Scores the job based on resume-related keywords.

6. Saves matching jobs to a CSV file.
"""
SEARCH_URL = "https://jobs.northropgrumman.com/careers?start=0&location=United+States&pid=1340071653903&sort_by=match&filter_include_remote=1&filter_experience_level=Entry"

# The exact clearance text we want to find inside each job description
CLEARANCE_TEXT = "CLEARANCE REQUIRED FOR START: No"

# Number of search result pages you said the site has
MAX_PAGES = 42

# Northrop appears to show about 10 jobs per page
JOBS_PER_PAGE = 10

# Keywords based on your resume, school projects, GitHub, and Bloodborne scraper project
# Higher numbers mean the keyword is more important
RESUME_KEYWORDS = {
    "python": 5,
    "software": 5,
    "cyber": 5,
    "security": 4,
    "information technology": 4,
    "computer science": 4,
    "github": 3,
    "git": 3,
    "linux": 3,
    "scripting": 4,
    "automation": 4,
    "debugging": 3,
    "object-oriented": 3,
    "oop": 3,
    "json": 2,
    "csv": 2,
    "html": 1,
    "data": 2,
    "database": 2,
    "sql": 2,
    "java": 2,
}


def clean_html_to_text(html):
    """
    Converts raw HTML into readable plain text.

    This removes script and style tags because they do not contain useful
    job description text.
    """

    soup = BeautifulSoup(html, "html.parser")

    for tag in soup(["script", "style"]):
        tag.decompose()

    return soup.get_text(" ", strip=True)


def score_job_against_resume(job_text):
    """
    Scores a job description based on how many resume-related keywords appear.

    Returns:
    - total score
    - list of matched keywords
    """

    job_text_lower = job_text.lower()

    score = 0
    matched_keywords = []

    for keyword, weight in RESUME_KEYWORDS.items():
        if keyword.lower() in job_text_lower:
            score += weight
            matched_keywords.append(keyword)

    return score, matched_keywords


def collect_job_ids(page):
    """
    Goes through all 42 Entry-level search result pages and collects job IDs.

    The search page is JavaScript-heavy, so Playwright is used instead of
    requests + BeautifulSoup.
    """

    job_ids = set()

    for page_number in range(MAX_PAGES):
        # Northrop uses the start value to move through pages.
        # Page 1 = start=0
        # Page 2 = start=10
        # Page 3 = start=20
        start_value = page_number * JOBS_PER_PAGE

        page_url = SEARCH_URL.replace("start=0", f"start={start_value}")

        print(f"Scanning page {page_number + 1}/{MAX_PAGES}")
        print(page_url)

        page.goto(page_url, wait_until="networkidle", timeout=60000)

        # Gives the page a little extra time to finish rendering job cards
        time.sleep(2)

        # Finds all links on the page that point to individual job postings
        job_links = page.locator("a").evaluate_all(
            """
            links => links
                .map(link => link.href)
                .filter(href => href.includes('/careers/job/'))
            """
        )

        # Extracts the job ID from URLs like:
        # https://jobs.northropgrumman.com/careers/job/1340071653903
        for link in job_links:
            job_id = link.rstrip("/").split("/")[-1].split("?")[0]

            if job_id.isdigit():
                job_ids.add(job_id)

        print(f"Unique job IDs found so far: {len(job_ids)}")
        print("-" * 60)

    return sorted(job_ids)


def analyze_single_job(page, job_id):
    """
    Opens one job page, reads the description, checks for clearance text,
    and scores the job against your resume keywords.
    """

    job_url = f"https://jobs.northropgrumman.com/careers/job/{job_id}"

    page.goto(job_url, wait_until="networkidle", timeout=60000)

    # Gives the job description time to load
    time.sleep(2)

    html = page.content()
    job_text = clean_html_to_text(html)

    # Checks whether the job contains the exact clearance requirement text
    has_clearance_no = CLEARANCE_TEXT.lower() in job_text.lower()

    # Scores the job based on your resume keywords
    fit_score, matched_keywords = score_job_against_resume(job_text)

    # Uses the browser page title as a simple job title
    title = page.title().replace(" | Northrop Grumman", "").strip()

    return {
        "job_id": job_id,
        "title": title,
        "url": job_url,
        "fit_score": fit_score,
        "matched_keywords": ", ".join(matched_keywords),
        "clearance_required_for_start_no": has_clearance_no,
    }


def main():
    """
    Main program flow:

    1. Opens the Northrop job search page with Playwright.
    2. Collects job IDs from all 42 Entry-level pages.
    3. Visits each job page.
    4. Keeps only jobs that:
       - contain CLEARANCE REQUIRED FOR START: No
       - match at least one resume keyword
    5. Saves results to a CSV file.
    """

    matching_jobs = []

    with sync_playwright() as playwright:
        # headless=False lets you see the browser.
        # This is useful because your resume/profile match may require login.
        browser = playwright.chromium.launch(headless=False)

        page = browser.new_page()

        job_ids = collect_job_ids(page)

        print(f"\nCollected {len(job_ids)} unique Entry-level job IDs.\n")

        for index, job_id in enumerate(job_ids, start=1):
            try:
                print(f"Checking job {index}/{len(job_ids)}: {job_id}")

                job = analyze_single_job(page, job_id)

                # This is the main filter.
                # The job must have no clearance required for start,
                # and it must match your resume at least slightly.
                if job["clearance_required_for_start_no"] and job["fit_score"] > 0:
                    matching_jobs.append(job)

                    print(f"MATCH FOUND: {job['title']}")
                    print(f"Fit Score: {job['fit_score']}")
                    print(f"Matched Keywords: {job['matched_keywords']}")

                print("-" * 60)

            except Exception as error:
                print(f"Error checking job {job_id}: {error}")

        browser.close()

    # Convert the final matching jobs list into a spreadsheet-like table
    df = pd.DataFrame(matching_jobs)

    # Sort strongest resume matches first
    if not df.empty:
        df = df.sort_values(by="fit_score", ascending=False)

    output_file = "northrop_entry_clearance_no_matches.csv"

    df.to_csv(output_file, index=False)

    print("\nScraping complete.")
    print(f"Saved {len(df)} matching jobs to {output_file}")


if __name__ == "__main__":
    main()