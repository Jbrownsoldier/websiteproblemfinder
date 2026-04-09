# main.py
# Pipeline orchestrator: CSV row → scrape → analyze → output row with websiteproblem.

import csv
import re
import time
from datetime import datetime, timezone
from typing import Callable, Optional

import config
from scraper import scrape_website
from analyzer import analyze_website


# ---------------------------------------------------------------------------
# Output columns
# ---------------------------------------------------------------------------

OUTPUT_COLUMNS = [
    "company_name",
    "website",
    "websiteproblem",
    "website_status",
    "notes",
    "generated_at",
]

# Statuses that mean the site is unreachable — skip Claude, use fallback
FATAL_STATUSES = {"connection_error", "timeout", "ssl_error", "blocked", "missing_url"}


# ---------------------------------------------------------------------------
# URL cleaning
# ---------------------------------------------------------------------------

def clean_website_url(raw: str) -> str:
    """
    Normalize a raw website string to https://domain.com format.
    Handles missing scheme, www variants, trailing slashes, and spaces.
    """
    if not raw:
        return ""

    url = raw.strip()

    # Remove common non-URL junk
    url = re.sub(r"\s+", "", url)

    # Add scheme if missing
    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = "https://" + url

    # Normalize scheme to lowercase
    url = re.sub(r"^HTTP://", "http://", url)
    url = re.sub(r"^HTTPS://", "https://", url)

    # Strip trailing slash
    url = url.rstrip("/")

    return url


# ---------------------------------------------------------------------------
# Row processing
# ---------------------------------------------------------------------------

def process_row(row: dict, api_key: str) -> dict:
    """
    Process one CSV row: scrape the website and identify its conversion problem.
    Returns a dict with all OUTPUT_COLUMNS populated.
    """
    company_name = (row.get("company_name") or "").strip()

    # Resolve website URL: prefer "website" column, fall back to "domain"
    raw_website = (row.get("website") or row.get("domain") or "").strip()
    website = clean_website_url(raw_website)

    output = {
        "company_name": company_name,
        "website":      website or raw_website,
        "websiteproblem": "",
        "website_status": "",
        "notes":        "",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    if not website:
        output["websiteproblem"] = config.FALLBACK_PROBLEM_SITE_DOWN
        output["website_status"] = "missing_url"
        output["notes"] = "No website or domain provided"
        return output

    # --- Scrape ---
    scrape_result = scrape_website(website)

    output["website_status"] = scrape_result.website_status
    output["notes"] = scrape_result.notes

    # Fatal scrape failure → use fallback, skip Claude
    if scrape_result.website_status in FATAL_STATUSES:
        output["websiteproblem"] = config.FALLBACK_PROBLEM_SITE_DOWN
        return output

    # No content returned (JS-only, empty pages) → use no-content fallback
    if not scrape_result.pages:
        output["websiteproblem"] = config.FALLBACK_PROBLEM_NO_CONTENT
        output["website_status"] = output["website_status"] or "no_content"
        return output

    # All pages are js_rendered_likely and produced no real text
    all_sparse = all(
        not p.text or len(p.text) < config.JS_RENDER_TEXT_THRESHOLD
        for p in scrape_result.pages
    )
    if all_sparse:
        output["websiteproblem"] = config.FALLBACK_PROBLEM_NO_CONTENT
        return output

    # --- Analyze with Claude ---
    websiteproblem = analyze_website(
        company_name=company_name,
        website=website,
        scrape_result=scrape_result,
        api_key=api_key,
    )
    output["websiteproblem"] = websiteproblem

    return output


# ---------------------------------------------------------------------------
# Pipeline runner
# ---------------------------------------------------------------------------

def run_pipeline(
    rows: list,
    output_path: str,
    api_key: str,
    progress_callback: Optional[Callable] = None,
) -> None:
    """
    Iterate all rows, call process_row() on each, write output CSV progressively.
    Calls progress_callback(current, total, company_name, analyzed, failed) after each row.
    """
    total = len(rows)
    analyzed = 0
    failed = 0

    with open(output_path, "w", newline="", encoding="utf-8") as fout:
        writer = csv.DictWriter(fout, fieldnames=OUTPUT_COLUMNS, extrasaction="ignore")
        writer.writeheader()

        for i, row in enumerate(rows, start=1):
            company_name = (row.get("company_name") or "").strip()

            try:
                result = process_row(row, api_key)

                # Track analyzed vs failed
                if result["websiteproblem"] in (
                    config.FALLBACK_PROBLEM_SITE_DOWN,
                    config.FALLBACK_PROBLEM_NO_CONTENT,
                    config.FALLBACK_PROBLEM_ANALYZE_ERROR,
                ):
                    failed += 1
                else:
                    analyzed += 1

                writer.writerow(result)

            except Exception as e:
                failed += 1
                writer.writerow({
                    "company_name":   company_name,
                    "website":        (row.get("website") or row.get("domain") or "").strip(),
                    "websiteproblem": config.FALLBACK_PROBLEM_ANALYZE_ERROR,
                    "website_status": "error",
                    "notes":          str(e),
                    "generated_at":   datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                })

            # Flush every PARTIAL_SAVE_EVERY rows for crash safety
            if i % config.PARTIAL_SAVE_EVERY == 0:
                fout.flush()

            if progress_callback:
                progress_callback(i, total, company_name, analyzed, failed)

            # Polite delay between companies
            if i < total:
                time.sleep(config.REQUEST_DELAY_SECONDS)
