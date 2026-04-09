# analyzer.py
# Two responsibilities:
#   1. Extract structural conversion signals from scraped HTML (pure BeautifulSoup, no API)
#   2. Call Claude to identify the single most important conversion problem

import re
from dataclasses import dataclass, field

import anthropic
from bs4 import BeautifulSoup

import config
from scraper import ScrapeResult


# ---------------------------------------------------------------------------
# Booking widget detection patterns
# ---------------------------------------------------------------------------

BOOKING_WIDGET_PATTERNS = {
    "Calendly":          r"calendly\.com",
    "Acuity Scheduling": r"acuityscheduling\.com",
    "Housecall Pro":     r"housecallpro\.com",
    "ServiceTitan":      r"servicetitan\.com",
    "SimplyBook":        r"simplybook\.me",
    "Booksy":            r"booksy\.com",
    "Vagaro":            r"vagaro\.com",
    "Square Appointments": r"square\.site|squareup\.com",
    "Appointy":          r"appointy\.com",
    "Setmore":           r"setmore\.com",
    "Mindbody":          r"mindbodyonline\.com",
    "Zocdoc":            r"zocdoc\.com",
    "Jane App":          r"jane\.app",
    "Bookeo":            r"bookeo\.com",
    "10to8":             r"10to8\.com",
}

CHAT_WIDGET_PATTERNS = {
    "Intercom":  r"intercom\.io|widget\.intercom\.io",
    "Drift":     r"drift\.com",
    "Tawk.to":   r"tawk\.to",
    "Tidio":     r"tidio\.com",
    "LiveChat":  r"livechatinc\.com|livechat\.com",
    "Crisp":     r"crisp\.chat",
    "Zendesk":   r"zopim\.com|zendesk\.com",
    "Freshchat": r"freshchat\.com|freshworks\.com",
}

# CTA verb patterns (case-insensitive match in button/link text)
CTA_VERBS = re.compile(
    r"\b(book|schedule|appointment|quote|estimate|call us|contact us|get started|"
    r"request|free consult|consult|sign up|get a quote|request a quote|"
    r"free estimate|get estimate|talk to us|reach out|message us)\b",
    re.IGNORECASE,
)

# Phone number patterns
PHONE_PATTERN = re.compile(
    r"(\+?1[\s\-.]?)?"
    r"(\(?\d{3}\)?[\s\-.]?)"
    r"(\d{3}[\s\-.]?)"
    r"(\d{4})"
)


# ---------------------------------------------------------------------------
# Signals data class
# ---------------------------------------------------------------------------

@dataclass
class ConversionSignals:
    has_phone_number: bool = False
    phone_numbers: list = field(default_factory=list)  # up to 3
    phone_in_header: bool = False                       # phone in first ~500 chars of text
    has_booking_widget: bool = False
    booking_widget_name: str = ""
    has_contact_form: bool = False
    cta_texts: list = field(default_factory=list)      # top 5 matching CTA button/link texts
    has_strong_cta: bool = False
    has_chat_widget: bool = False
    chat_widget_name: str = ""
    has_email_capture: bool = False
    booking_page_found: bool = False
    contact_page_found: bool = False
    pages_scraped: int = 0


# ---------------------------------------------------------------------------
# Signal extraction
# ---------------------------------------------------------------------------

def extract_signals(scrape_result: ScrapeResult) -> ConversionSignals:
    """
    Parse homepage HTML with BeautifulSoup to extract structural conversion signals.
    Pure regex + HTML parsing — no API call.
    """
    signals = ConversionSignals()
    signals.pages_scraped = len(scrape_result.pages)

    # Mark which page paths were successfully fetched
    fetched_paths = {p.page_path for p in scrape_result.pages if p.website_status == "ok"}
    booking_paths = {"/book", "/booking", "/schedule", "/appointments",
                     "/quote", "/get-a-quote", "/request-a-quote", "/free-quote", "/estimate"}
    contact_paths = {"/contact", "/contact-us"}

    signals.booking_page_found = bool(fetched_paths & booking_paths)
    signals.contact_page_found  = bool(fetched_paths & contact_paths)

    # Work primarily from homepage; supplement with other pages for HTML signals
    homepage = scrape_result.homepage
    if not homepage:
        return signals

    html = homepage.html or ""
    text = homepage.text or ""

    # --- Phone number detection ---
    phones = PHONE_PATTERN.findall(text)
    if phones:
        # Reconstruct full phone strings from groups and deduplicate
        seen = set()
        for match in re.finditer(PHONE_PATTERN, text):
            num = match.group(0).strip()
            if num not in seen:
                seen.add(num)
                signals.phone_numbers.append(num)
            if len(signals.phone_numbers) >= 3:
                break
        signals.has_phone_number = True
        # Check if phone appears in the first 500 chars (header/nav area)
        signals.phone_in_header = bool(PHONE_PATTERN.search(text[:500]))

    # --- Booking widget detection (check raw HTML for script/iframe src) ---
    for widget_name, pattern in BOOKING_WIDGET_PATTERNS.items():
        if re.search(pattern, html, re.IGNORECASE):
            signals.has_booking_widget = True
            signals.booking_widget_name = widget_name
            break

    # --- Chat widget detection ---
    for widget_name, pattern in CHAT_WIDGET_PATTERNS.items():
        if re.search(pattern, html, re.IGNORECASE):
            signals.has_chat_widget = True
            signals.chat_widget_name = widget_name
            break

    # --- Contact form detection (BeautifulSoup) ---
    try:
        soup = BeautifulSoup(html, "lxml")

        # Remove booking widget iframes from soup before form detection
        for iframe in soup.find_all("iframe"):
            src = iframe.get("src", "")
            for pattern in BOOKING_WIDGET_PATTERNS.values():
                if re.search(pattern, src, re.IGNORECASE):
                    iframe.decompose()
                    break

        forms = soup.find_all("form")
        for form in forms:
            inputs = form.find_all("input")
            input_types = [i.get("type", "").lower() for i in inputs]
            input_names = [i.get("name", "").lower() for i in inputs]
            input_placeholders = [i.get("placeholder", "").lower() for i in inputs]
            all_attrs = " ".join(input_types + input_names + input_placeholders)
            # A lead-capture form has email or phone inputs
            if re.search(r"email|phone|tel|mobile|name|message", all_attrs):
                signals.has_contact_form = True
                break

        # Check for email capture (newsletter / lead magnet forms)
        if not signals.has_contact_form:
            for form in forms:
                form_text = form.get_text(" ", strip=True).lower()
                if re.search(r"subscribe|newsletter|notify|email.*sign|sign.*up", form_text):
                    signals.has_email_capture = True
                    break

        # --- CTA button/link detection ---
        cta_elements = soup.find_all(["a", "button"])
        seen_texts = set()
        for el in cta_elements:
            el_text = el.get_text(" ", strip=True).strip()
            if not el_text or len(el_text) > 80:
                continue
            if el_text.lower() in seen_texts:
                continue
            if CTA_VERBS.search(el_text):
                seen_texts.add(el_text.lower())
                signals.cta_texts.append(el_text)
                if len(signals.cta_texts) >= 5:
                    break

        signals.has_strong_cta = bool(signals.cta_texts)

    except Exception:
        pass

    return signals


def signals_to_text(signals: ConversionSignals) -> str:
    """Format ConversionSignals as a plain-text block for the Claude prompt."""
    lines = []

    # Phone
    if signals.has_phone_number:
        nums = ", ".join(signals.phone_numbers[:3]) if signals.phone_numbers else "found"
        header_note = " — in header/nav: YES" if signals.phone_in_header else " — in header/nav: NO (may be buried)"
        lines.append(f"Phone number: YES ({nums}){header_note}")
    else:
        lines.append("Phone number: NOT FOUND")

    # Booking widget
    if signals.has_booking_widget:
        lines.append(f"Online booking widget: YES ({signals.booking_widget_name})")
    else:
        lines.append("Online booking widget: NOT DETECTED")

    # Contact form
    lines.append(f"Contact/lead form: {'YES' if signals.has_contact_form else 'NOT FOUND'}")

    # Email capture
    if signals.has_email_capture:
        lines.append("Email capture form: YES (newsletter/subscribe)")

    # Chat widget
    if signals.has_chat_widget:
        lines.append(f"Chat widget: YES ({signals.chat_widget_name})")
    else:
        lines.append("Chat widget: NOT DETECTED")

    # CTA buttons
    if signals.cta_texts:
        ctas = ", ".join(f'"{t}"' for t in signals.cta_texts[:5])
        lines.append(f"Conversion CTA buttons/links found: {ctas}")
    else:
        lines.append("Conversion CTA buttons/links: NONE DETECTED")

    # Subpages
    lines.append(f"Booking page (/book, /schedule, /quote, etc.): {'FOUND' if signals.booking_page_found else 'NOT FOUND'}")
    lines.append(f"Contact page (/contact, /contact-us): {'FOUND' if signals.contact_page_found else 'NOT FOUND'}")
    lines.append(f"Pages analyzed: {signals.pages_scraped}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Page text trimming
# ---------------------------------------------------------------------------

def trim_page_text(scrape_result: ScrapeResult) -> str:
    """
    Combine homepage text + best subpage text, labeled and trimmed to MAX_PAGE_TEXT_CHARS.
    Keeps above-fold content (start of homepage) and the most conversion-relevant subpage.
    """
    parts = []

    homepage = scrape_result.homepage
    if homepage and homepage.text:
        hp_text = homepage.text[:config.HOMEPAGE_TEXT_CHARS]
        parts.append(f"[HOMEPAGE]\n{hp_text}")

    # Find the best subpage: prefer booking/contact paths, then longest text
    priority_paths = {"/book", "/booking", "/schedule", "/appointments",
                      "/quote", "/get-a-quote", "/request-a-quote",
                      "/contact", "/contact-us", "/services"}

    subpages = [p for p in scrape_result.pages if p.page_path != "/" and p.text]
    if subpages:
        # Sort: priority paths first, then by text length descending
        def subpage_rank(p):
            return (0 if p.page_path in priority_paths else 1, -len(p.text))
        best_sub = sorted(subpages, key=subpage_rank)[0]
        sub_text = best_sub.text[:config.SUBPAGE_TEXT_CHARS]
        label = best_sub.page_path.upper().replace("/", "").replace("-", " ") or "SUBPAGE"
        parts.append(f"[{label} PAGE]\n{sub_text}")

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Claude prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a conversion rate specialist analyzing business websites for local service companies (plumbers, roofers, dentists, lawyers, contractors, landscapers, etc.).

Your job: identify the single most important conversion problem on this website — the one issue closest to lost revenue.

Conversion actions ranked by priority (highest value first):
1. Book an appointment
2. Request a quote or estimate
3. Call the business
4. Submit a contact or lead form
5. Start a chat
6. Submit email for follow-up
7. Click a strong CTA moving toward conversion

Decision rules:
- If the site lacks a direct path to the highest-value action entirely, that is the problem
- If the action exists but is weak, buried, unclear, or hard to find, that is the problem
- If no strong money-adjacent action exists, identify the missing lead capture or weak CTA problem
- Ignore all issues that are less important than the main conversion blocker

Output format rules (strictly enforced):
- Return ONLY the websiteproblem value — nothing else, no explanation
- No quotation marks, no bullets, no period at the end
- Must fit naturally in the sentence: "I noticed your website is [websiteproblem]"
- Be concise, specific, and natural — as if spoken in a cold outreach message
- Do NOT mention page speed, technical SEO, branding, colors, or design unless directly tied to conversion loss

Good output examples:
  missing a way to book appointments online
  hiding the phone number below the fold
  missing a contact form on the contact page
  using a vague "Submit" button instead of a clear call to action
  missing any way to request a quote
  making visitors scroll past three sections of content before seeing any contact option
  lacking a dedicated booking or scheduling page
  not showing a phone number anywhere on the site"""

USER_PROMPT_TEMPLATE = """WEBSITE: {website}
COMPANY: {company_name}

--- STRUCTURAL SIGNALS (extracted from HTML) ---
{signals_block}

--- PAGE TEXT (trimmed, homepage + key subpage) ---
{page_text}

Return only the websiteproblem value."""


# ---------------------------------------------------------------------------
# Rule-based analysis (runs first — no API key required)
# ---------------------------------------------------------------------------

def rule_based_analysis(signals: ConversionSignals) -> "str | None":
    """
    Apply deterministic rules to identify obvious conversion gaps.
    Returns a websiteproblem string when the problem is clear-cut.
    Returns None when the situation is ambiguous — caller should invoke Claude.

    Priority order mirrors the conversion hierarchy:
    book > quote > call > form > chat > email > CTA
    """
    has_any_path = (
        signals.has_booking_widget
        or signals.has_contact_form
        or signals.has_phone_number
        or signals.has_chat_widget
    )

    # 1. Absolutely nothing — no way to contact or convert
    if not has_any_path and not signals.has_strong_cta:
        return "missing any way for visitors to contact or reach out"

    # 2. No phone, no booking widget, no contact form — only maybe a CTA link
    if not signals.has_phone_number and not signals.has_booking_widget and not signals.has_contact_form:
        return "missing a phone number, contact form, or any way for visitors to reach out"

    # 3. No booking widget and no contact form (phone may exist but that's it)
    if not signals.has_booking_widget and not signals.has_contact_form:
        if signals.has_phone_number and not signals.phone_in_header:
            return "hiding the phone number below the fold with no form or booking option"
        if signals.has_phone_number:
            return "missing a contact form or online booking option"

    # 4. Has contact form but no booking widget and phone is buried (or absent)
    if not signals.has_booking_widget:
        if not signals.has_phone_number:
            if signals.has_contact_form and not signals.has_strong_cta:
                return "burying the contact form with no clear call to action directing visitors to it"
            if signals.has_contact_form:
                # Form + CTAs exist, no phone, no booking — ambiguous enough for Claude
                return None
        elif not signals.phone_in_header:
            if not signals.has_strong_cta:
                return "hiding the phone number below the fold and missing a clear call to action"
            # Phone buried but form + CTAs present — let Claude judge
            return None

    # 5. No CTAs anywhere and no booking widget
    if not signals.has_strong_cta and not signals.has_booking_widget:
        return "lacking any clear call to action for ready-to-buy visitors"

    # 6. All the obvious elements are present — problem is subtle
    #    Let Claude read the actual page content and judge
    return None


# ---------------------------------------------------------------------------
# Claude API call
# ---------------------------------------------------------------------------

def _call_claude(system: str, user: str, api_key: str) -> str:
    client = anthropic.Anthropic(api_key=api_key)
    message = client.messages.create(
        model=config.CLAUDE_MODEL,
        max_tokens=60,
        temperature=0,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return message.content[0].text.strip()


def _normalize_output(raw: str) -> str:
    """Strip quotes, take first line/sentence, lowercase first character."""
    # Take first line only
    raw = raw.split("\n")[0].strip()
    # Take first sentence if multi-sentence
    if ". " in raw:
        raw = raw.split(". ")[0].strip()
    # Strip surrounding quotes
    raw = raw.strip("\"'")
    # Strip trailing period
    raw = raw.rstrip(".")
    raw = raw.strip()
    # Lowercase first character so it flows as "...is [missing a contact form]"
    if raw:
        raw = raw[0].lower() + raw[1:]
    return raw


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def analyze_website(
    company_name: str,
    website: str,
    scrape_result: ScrapeResult,
    api_key: str = "",
) -> str:
    """
    Identify the single most important conversion problem on the website.

    Hybrid approach:
    1. Extract structural signals (free, no API)
    2. Run rule-based analysis — returns immediately if problem is obvious
    3. Only call Claude when signals are ambiguous (requires api_key)

    Returns the websiteproblem string ready for a CSV column.
    """
    try:
        signals = extract_signals(scrape_result)

        # --- Step 1: try rule engine first (free, instant) ---
        rule_result = rule_based_analysis(signals)
        if rule_result is not None:
            return rule_result

        # --- Step 2: ambiguous — call Claude if a key was provided ---
        if not api_key:
            # No key available; fall back to a generic but honest signal-based output
            if not signals.has_strong_cta:
                return "lacking a clear call to action that guides visitors toward conversion"
            return "making it unclear how visitors should take the next step"

        page_text = trim_page_text(scrape_result)
        if not page_text.strip():
            return config.FALLBACK_PROBLEM_NO_CONTENT

        signals_block = signals_to_text(signals)
        user_prompt = USER_PROMPT_TEMPLATE.format(
            website=website,
            company_name=company_name,
            signals_block=signals_block,
            page_text=page_text,
        )

        raw = _call_claude(SYSTEM_PROMPT, user_prompt, api_key)
        return _normalize_output(raw) or config.FALLBACK_PROBLEM_ANALYZE_ERROR

    except Exception:
        return config.FALLBACK_PROBLEM_ANALYZE_ERROR
