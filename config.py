# config.py
# Central configuration for the Website Problem Finder.

# ---------------------------------------------------------------------------
# Claude / AI settings
# ---------------------------------------------------------------------------

CLAUDE_MODEL = "claude-sonnet-4-5"

# Your Anthropic API key. Can be set here or entered in the web UI each run.
ANTHROPIC_API_KEY = ""

# ---------------------------------------------------------------------------
# HTTP / Scraping settings
# ---------------------------------------------------------------------------

REQUEST_DELAY_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 10
MAX_RETRIES = 2
PARTIAL_SAVE_EVERY = 25

# Minimum visible text length (chars) before we consider a page JS-rendered
JS_RENDER_TEXT_THRESHOLD = 200

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

# ---------------------------------------------------------------------------
# Jina.ai Reader (free JS fallback)
# ---------------------------------------------------------------------------

USE_JINA_READER = True
JINA_REQUEST_TIMEOUT = 15

# ---------------------------------------------------------------------------
# Pages to scrape for conversion signal analysis (tried in order)
# ---------------------------------------------------------------------------

PAGES_TO_TRY = [
    "/",
    "/contact",
    "/contact-us",
    "/book",
    "/booking",
    "/schedule",
    "/appointments",
    "/quote",
    "/get-a-quote",
    "/request-a-quote",
    "/free-quote",
    "/estimate",
    "/services",
    "/about",
]

# Maximum number of subpages to analyze beyond homepage
MAX_PAGES_TO_ANALYZE = 3

# ---------------------------------------------------------------------------
# Claude prompt content limits
# ---------------------------------------------------------------------------

# Total characters of page text sent to Claude
MAX_PAGE_TEXT_CHARS = 3000

# Characters from homepage to include (above-fold content, nav, hero)
HOMEPAGE_TEXT_CHARS = 1500

# Characters from the best subpage found
SUBPAGE_TEXT_CHARS = 1500

# ---------------------------------------------------------------------------
# Fallback websiteproblem values (when site is unreachable or unanalyzable)
# ---------------------------------------------------------------------------

FALLBACK_PROBLEM_SITE_DOWN   = "currently showing a broken or unreachable website"
FALLBACK_PROBLEM_NO_CONTENT  = "not loading properly for visitors without JavaScript"
FALLBACK_PROBLEM_ANALYZE_ERROR = "unable to be analyzed at this time"
