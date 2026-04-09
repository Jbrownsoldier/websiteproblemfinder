# scraper.py
# Fetches website pages and returns HTML + visible text + status metadata.
# Uses requests for static HTML, with Jina.ai Reader as free JS fallback.

import re
import time
import requests
from bs4 import BeautifulSoup
from dataclasses import dataclass, field

import config


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class PageResult:
    url: str = ""
    page_path: str = ""       # e.g. "/contact", "/" for homepage
    html: str = ""
    text: str = ""            # Visible text stripped of tags
    status_code: int = 0
    website_status: str = ""  # "ok" | "not_found" | "blocked" | "timeout" | "connection_error" | "ssl_error" | "js_rendered_likely" | "error"
    error: str = ""
    jina_used: bool = False


@dataclass
class ScrapeResult:
    pages: list = field(default_factory=list)   # list[PageResult] — all successfully fetched pages
    homepage: "PageResult | None" = None
    website_status: str = ""
    notes: str = ""
    jina_used: bool = False


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": config.USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "keep-alive",
    })
    return session


def _extract_visible_text(html: str) -> str:
    """Strip HTML tags and return clean visible text."""
    try:
        soup = BeautifulSoup(html, "lxml")
        for tag in soup(["script", "style", "noscript", "meta", "head"]):
            tag.decompose()
        text = soup.get_text(separator=" ", strip=True)
        text = re.sub(r"\s+", " ", text).strip()
        return text
    except Exception:
        return ""


def _looks_js_rendered(text: str) -> bool:
    """Return True if the page text is too short to contain real content."""
    return len(text) < config.JS_RENDER_TEXT_THRESHOLD


def _fetch_with_jina(url: str, page_path: str = "") -> PageResult:
    """
    Fetch a URL via Jina.ai Reader (r.jina.ai/<url>).
    Returns clean readable text from JS-rendered sites — free, no key required.
    """
    result = PageResult(url=url, page_path=page_path)
    try:
        jina_url = f"https://r.jina.ai/{url}"
        resp = requests.get(
            jina_url,
            headers={
                "User-Agent": config.USER_AGENT,
                "Accept": "text/plain, text/markdown, */*",
                "X-Return-Format": "text",
            },
            timeout=config.JINA_REQUEST_TIMEOUT,
        )

        if resp.status_code != 200 or not resp.text.strip():
            result.website_status = "error"
            result.error = f"jina_http_{resp.status_code}"
            return result

        text = resp.text.strip()
        result.html = text
        result.text = text
        result.status_code = 200
        result.jina_used = True
        result.website_status = "js_rendered_likely" if _looks_js_rendered(text) else "ok"
        return result

    except Exception as e:
        result.website_status = "error"
        result.error = f"jina_error: {e}"
        return result


def _fetch_url(session: requests.Session, url: str, page_path: str = "") -> PageResult:
    """
    Fetch a single URL. Retries up to MAX_RETRIES on connection/timeout errors.
    Auto-triggers Jina fallback when static text is too sparse.
    """
    result = PageResult(url=url, page_path=page_path)
    attempts = 0

    while attempts <= config.MAX_RETRIES:
        try:
            resp = session.get(url, timeout=config.REQUEST_TIMEOUT_SECONDS, allow_redirects=True)
            result.status_code = resp.status_code

            if resp.status_code == 200:
                result.html = resp.text
                result.text = _extract_visible_text(resp.text)

                if _looks_js_rendered(result.text) and config.USE_JINA_READER:
                    jina = _fetch_with_jina(url, page_path)
                    if jina.website_status == "ok":
                        return jina
                    # Jina failed — keep the sparse static result
                    result.website_status = "js_rendered_likely"
                else:
                    result.website_status = "ok"

            elif resp.status_code in (403, 429, 503):
                result.website_status = "blocked"
                result.error = f"HTTP {resp.status_code}"
            elif resp.status_code == 404:
                result.website_status = "not_found"
                result.error = "404"
            else:
                result.website_status = "error"
                result.error = f"HTTP {resp.status_code}"

            return result

        except requests.exceptions.Timeout:
            attempts += 1
            result.error = "timeout"
            if attempts > config.MAX_RETRIES:
                result.website_status = "timeout"
                return result
            time.sleep(1)

        except requests.exceptions.SSLError:
            if url.startswith("https://"):
                url = "http://" + url[8:]
                result.url = url
                attempts += 1
                continue
            result.website_status = "ssl_error"
            result.error = "ssl_error"
            return result

        except requests.exceptions.ConnectionError as e:
            attempts += 1
            result.error = f"connection_error: {e}"
            if attempts > config.MAX_RETRIES:
                result.website_status = "connection_error"
                return result
            time.sleep(1)

        except Exception as e:
            result.website_status = "error"
            result.error = str(e)
            return result

    result.website_status = "error"
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def scrape_website(website: str) -> ScrapeResult:
    """
    Fetch the homepage and conversion-relevant subpages for a business website.
    Returns a ScrapeResult with all successfully fetched pages.
    """
    result = ScrapeResult()

    if not website:
        result.website_status = "missing_url"
        result.notes = "No website URL provided"
        return result

    base_url = website.rstrip("/")
    session = _make_session()

    # Fetch homepage first — always included
    home = _fetch_url(session, base_url, page_path="/")

    if home.website_status in ("connection_error", "timeout", "ssl_error", "blocked"):
        result.website_status = home.website_status
        result.notes = f"Homepage unreachable: {home.error}"
        result.homepage = home
        return result

    if home.website_status == "ok" and home.text:
        result.homepage = home
        result.pages.append(home)
    elif home.website_status == "js_rendered_likely":
        result.homepage = home
        result.pages.append(home)

    # Fetch subpages up to MAX_PAGES_TO_ANALYZE additional pages
    subpages_found = 0
    for path in config.PAGES_TO_TRY:
        if path == "/":
            continue  # already fetched above
        if subpages_found >= config.MAX_PAGES_TO_ANALYZE:
            break

        url = base_url + path
        page = _fetch_url(session, url, page_path=path)

        if page.website_status == "ok" and page.text:
            result.pages.append(page)
            subpages_found += 1
        elif page.website_status in ("connection_error", "timeout", "blocked"):
            # Site is actively blocking — stop hammering it
            break

        time.sleep(0.3)  # light throttle between subpage fetches

    result.jina_used = any(p.jina_used for p in result.pages)

    if not result.pages:
        result.website_status = "no_content"
        result.notes = "No accessible pages returned any content"
    else:
        result.website_status = result.pages[0].website_status

    return result
