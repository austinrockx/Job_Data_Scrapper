# northrop_scraper.py

import re
import csv
import time
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin

BASE_URL = "https://jobs.northropgrumman.com"
SEARCH_URL = "https://jobs.northropgrumman.com/careers/jobs"

HEADERS = {
    "User-Agent": "Mozilla/5.0"
}

RESUME_KEYWORDS = {
    "python": 5,
    "software": 4,
    "cyber": 5,
    "security": 4,
    "object-oriented": 3,
    "oop": 3,
    "git": 3,
    "github": 3,
    "json": 2,
    "csv": 2,
    "linux": 3,
    "scripting": 4,
    "debugging": 3,
    "data": 2,
    "java": 2,
    "sql": 2,
    "html": 1,
    "scraper": 3,
    "automation": 4,
    "bloodborne": 2,
}

CLEARANCE_TEXT = "CLEARANCE REQUIRED FOR START: No"


def get_html(url):
    response = requests.get(url, headers=HEADERS, timeout=20)
    response.raise_for_status()
    return response.text


def clean_text(html):
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup(["script", "style"]):
        tag.decompose()

    return soup.get_text(" ", strip=True)


def extract_job_links(search_html):
    soup = BeautifulSoup(search_html, "html.parser")
    links = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]

        if "/careers/job/" in href:
            links.add(urljoin(BASE_URL, href))

    return sorted(links)


def score_resume_fit(text):
    text_lower = text.lower()
    score = 0
    matched_keywords = []

    for keyword, weight in RESUME_KEYWORDS.items():
        if keyword.lower() in text_lower:
            score += weight
            matched_keywords.append(keyword)

    return score, matched_keywords


def extract_title(text):
    # Basic fallback title extraction
    match = re.search(r"([A-Z][A-Za-z\s/-]+(?:Engineer|Analyst|Developer|Specialist|Administrator)[A-Za-z\s/-]*)", text)
    return match.group(1).strip() if match else "Unknown Title"


def analyze_job(url):
    html = get_html(url)
    text = clean_text(html)

    clearance_required_no = CLEARANCE_TEXT.lower() in text.lower()
    score, matched_keywords = score_resume_fit(text)
    title = extract_title(text)

    return {
        "title": title,
        "url": url,
        "fit_score": score,
        "matched_keywords": ", ".join(matched_keywords),
        "clearance_required_for_start_no": clearance_required_no,
        "contains_clearance_text": CLEARANCE_TEXT if clearance_required_no else "",
    }


def main():
    search_html = get_html(SEARCH_URL)
    job_links = extract_job_links(search_html)

    print(f"Found {len(job_links)} job links.")

    results = []

    for url in job_links:
        try:
            job = analyze_job(url)

            if job["clearance_required_for_start_no"] and job["fit_score"] >= 8:
                results.append(job)
                print(f"MATCH: {job['title']} | Score: {job['fit_score']}")

            time.sleep(1)

        except Exception as e:
            print(f"Error scraping {url}: {e}")

    results.sort(key=lambda x: x["fit_score"], reverse=True)

    with open("northrop_matches.csv", "w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=[
                "title",
                "fit_score",
                "matched_keywords",
                "contains_clearance_text",
                "url",
            ],
        )
        writer.writeheader()
        writer.writerows(results)

    print(f"Saved {len(results)} matching jobs to northrop_matches.csv")


if __name__ == "__main__":
    main()