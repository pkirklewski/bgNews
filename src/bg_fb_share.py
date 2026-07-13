#!/usr/bin/env python3
"""
Facebook Share-to-Feed Bot for Boguszow-Gorce

Monitors 4 Facebook pages for new posts and shares them to the
"Boguszow-Gorce Newsy i Informacje" page feed.

Scraping: Playwright (async, headless) - handles JS-heavy Facebook pages
Sharing: Selenium via Docker container (already logged in as the page)

Usage:
    python src/bg_fb_share.py
"""

import asyncio
import json
import re
import logging
import sys
import os
import time
import random
import fcntl
import atexit
import traceback
from datetime import datetime, timedelta
from pathlib import Path

from playwright.async_api import async_playwright
from bs4 import BeautifulSoup

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.action_chains import ActionChains

# ============================================
# CONFIGURATION
# ============================================

TEST_MODE = False
USE_DOCKER = True

PROJECT_ROOT = Path(__file__).parent.parent
LOG_FILE = PROJECT_ROOT / "logs" / "bg_fb_share.log"
SHARED_POSTS_FILE = PROJECT_ROOT / "data" / "shared_posts.json"
# Failed-share dedupe: URLs that failed share recently. Prevents the same
# unshareable URL from being retried every 4 hours indefinitely (e.g. if
# FB changes a Reel's share UI, our selectors miss for a day, but we
# don't waste Selenium time on the same Reel 6x/day). TTL via
# FAILED_SHARE_RETRY_HOURS — after that we'll try again in case FB
# changed back or we shipped a selector fix.
FAILED_SHARES_FILE = PROJECT_ROOT / "data" / "failed_shares.json"
FAILED_SHARE_RETRY_HOURS = 24

# Max posts to harvest from a single source per scrape run. Prevents
# accidental burst (e.g. fresh deploy seeing 20 posts from one source
# would try to share all of them in a single run, risking FB antispam
# flag). At steady state most sources have 0-1 new posts per cron so
# this cap is rarely hit; on first deploy it bounds the catch-up.
#
# Bumped 3→7 on 2026-06-30 after diagnosis: with cap=3, when Burmistrz
# (or any prolific source) posted 2-3 things in a single day, older
# posts from earlier that day permanently dropped below the cap and
# were never shared. Concrete miss: Burmistrz fbid 122114608305
# ("uczniowie klas ósmych z PSP nr 6 w gabinecie"), present as
# posinset=7 today but never harvested because we only ever kept
# the top 3 (posinsets 4-6). Dedupe via shared_posts.json prevents
# the wider window from causing duplicate shares — it only widens
# our backfill catch-up.
MAX_POSTS_PER_SOURCE = 7

# URLs that require authentication to scrape (FB blocks unauth Playwright
# from reading them). Detected by URL pattern: profile.php?id=... maps to
# "new pages experience" profiles which behave differently than /handle/
# pages in unauth scraping. Routed through Selenium auth path instead.
def _requires_auth_scrape(url: str) -> bool:
    return 'profile.php?id=' in url
LOCK_FILE = PROJECT_ROOT / "locks" / "fb_share.lock"
DEBUG_DIR = PROJECT_ROOT / "debug"

FB_PAGE_URL = "https://www.facebook.com/profile.php?id=100027689516729"
FB_PAGE_NAME = "Boguszow-Gorce Newsy i Informacje"

# Facebook pages to monitor for new posts.
# Mix of /handle URLs and /profile.php?id=... URLs (the latter for "new
# pages experience" professional accounts that don't expose a vanity
# handle). Playwright unauthenticated scraping works for both — public
# posts only.
#
# IMPORTANT: this list controls inbound aggregation (we read FROM these,
# repost ON our page). It is DISTINCT from RCB_ALERT_EXTRA_PROFILE_POSTS
# in bg_weather_map_selenium.py (which controls outbound alert
# distribution TO designated profiles).
MONITORED_PAGES = [
    # --- Municipal / official ---
    {"name": "Gmina Miasto Boguszow-Gorce",       "url": "https://www.facebook.com/gminamiastoboguszowgorce"},
    {"name": "Burmistrz Daniel Lubinski",         "url": "https://www.facebook.com/profile.php?id=61583693225846"},
    {"name": "Straz Miejska Boguszow-Gorce",      "url": "https://www.facebook.com/profile.php?id=100065918171599"},
    {"name": "OSP Boguszow",                       "url": "https://www.facebook.com/ospboguszow"},
    # --- Culture / education ---
    {"name": "MBPCK (biblioteka + centrum kultury)", "url": "https://www.facebook.com/MBPCK"},
    {"name": "Zespol Szkolno-Przedszkolny",       "url": "https://www.facebook.com/profile.php?id=100063549236963"},
    # --- Sport / recreation ---
    {"name": "OSiR Boguszow-Gorce",                "url": "https://www.facebook.com/osirbg"},
    {"name": "Gornik Boguszow-Gorce",              "url": "https://www.facebook.com/GornikBoguszowGorce"},
    {"name": "HEROS Boguszow-Gorce (zapasy)",      "url": "https://www.facebook.com/zapasyheros"},
    {"name": "Stajnia Boguszow",                   "url": "https://www.facebook.com/stajniaboguszow"},
    # --- Local establishments ---
    {"name": "Stodola Dzika",                      "url": "https://www.facebook.com/StodolaDzika"},
    {"name": "Osrodek Gora Dzikowiec",             "url": "https://www.facebook.com/OSRDzikowiec"},
    # --- Religious communities ---
    # REMOVED 2026-07-13 (user request): "Kościół Uliczny Boguszów-Gorce"
    # (profile.php?id=100067837419514). The page hadn't posted anything new
    # in months but its old 2021-2022 photos were repeatedly re-scraped
    # after DAYS_TO_KEEP prune expired their shared_posts.json entries.
    # Net effect: bgnews wall got a wave of years-old religious posts
    # every ~7 days. NEVER add this profile back — user's ask is a hard
    # no. See BLOCKED_SOURCE_URLS below (defensive guard).
    # --- TODO: WEBSITE sources (non-FB) handled by bg_scraper_selenium.py
    #     extension. Pending: https://bip.boguszow-gorce.pl/ (BIP)
]

# Sources that must NEVER be automatically posted to bgnews wall,
# regardless of what MONITORED_PAGES / MONITORED_GROUPS may contain.
# Enforced by a hard guard in the scrape pipeline. User can add MONITORED
# entries freely, but anything matching a BLOCKED_SOURCE_URLS token is
# stripped before share.
BLOCKED_SOURCE_URLS = {
    # Kościół Uliczny Boguszów-Gorce — user removed 2026-07-13 after a
    # re-share incident of years-old 2021-2022 posts.
    "profile.php?id=100067837419514",
    "100067837419514",
}

# FB GROUPS to monitor (require authentication — scraped via the same
# Selenium driver that handles share, after ensure_logged_in_as_page).
# Group post permalinks are /groups/<gid>/posts/<pid>. parse_fb_posts()
# also matches aria-posinset on group post divs.
#
# CAVEAT: closed-group posts may not be re-shareable to our page (FB
# only shows Share button on public-group posts in many cases). The
# scrape happens regardless; share_post() will fail gracefully if the
# Share button isn't available for a given group post.
MONITORED_GROUPS = [
    {"name": "Kosciol Zielonoswiatkowy Boguszow-Gorce",
     "url": "https://www.facebook.com/groups/799540943412790"},
]

# Delays between shares (seconds)
MIN_DELAY_BETWEEN_SHARES = 15
MAX_DELAY_BETWEEN_SHARES = 30

# How long to remember an already-shared URL in shared_posts.json.
# BUMPED 2026-07-13 (7 → 365) after Kościół Uliczny incident: a page with
# no new activity had its 2021-2022 posts scraped anew, re-shared, then
# prune-expired at day 7, then re-scraped at day 14, re-shared again.
# One year of memory is enough to cover any quiet-page cycle we care
# about; state file grows ~1 KB per shared URL so a year of daily shares
# = ~10 MB, trivially manageable.
DAYS_TO_KEEP = 365

# Ensure directories exist
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
SHARED_POSTS_FILE.parent.mkdir(parents=True, exist_ok=True)
LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
DEBUG_DIR.mkdir(parents=True, exist_ok=True)

# Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


# ============================================
# PROCESS ISOLATION & LOCKING
# ============================================

_lock_file_handle = None


def acquire_script_lock():
    """Acquire exclusive lock to prevent concurrent script runs.

    Returns True if lock acquired, False if another instance is running.
    """
    global _lock_file_handle

    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)

    try:
        _lock_file_handle = open(LOCK_FILE, 'w')
        fcntl.flock(_lock_file_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_file_handle.write(f"{os.getpid()}\n{datetime.now().isoformat()}\n")
        _lock_file_handle.flush()
        logger.info("Script lock acquired")
        return True
    except (IOError, OSError) as e:
        if _lock_file_handle:
            _lock_file_handle.close()
            _lock_file_handle = None
        logger.error(f"Could not acquire lock - another instance may be running: {e}")
        return False


def release_script_lock():
    """Release the script lock."""
    global _lock_file_handle

    if _lock_file_handle:
        try:
            fcntl.flock(_lock_file_handle.fileno(), fcntl.LOCK_UN)
            _lock_file_handle.close()
            _lock_file_handle = None
            logger.info("Script lock released")
        except Exception as e:
            logger.warning(f"Error releasing lock: {e}")


# ============================================
# HUMAN-LIKE HELPERS
# ============================================

def human_delay(min_sec=0.5, max_sec=2.0):
    """Random delay to mimic human behavior."""
    time.sleep(random.uniform(min_sec, max_sec))


# ============================================
# SHARED POSTS PERSISTENCE (DEDUPLICATION)
# ============================================

def load_shared_posts() -> dict:
    """Load previously shared posts from file."""
    if SHARED_POSTS_FILE.exists():
        try:
            with open(SHARED_POSTS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {}
    return {}


def save_shared_posts(shared_posts: dict) -> None:
    """Save shared posts to file."""
    with open(SHARED_POSTS_FILE, 'w', encoding='utf-8') as f:
        json.dump(shared_posts, f, indent=2, ensure_ascii=False)


def cleanup_old_posts(shared_posts: dict) -> dict:
    """Remove posts older than DAYS_TO_KEEP."""
    cutoff = datetime.now() - timedelta(days=DAYS_TO_KEEP)
    cutoff_str = cutoff.isoformat()

    cleaned = {
        post_url: timestamp
        for post_url, timestamp in shared_posts.items()
        if timestamp > cutoff_str
    }

    removed_count = len(shared_posts) - len(cleaned)
    if removed_count > 0:
        logger.info(f"Cleaned up {removed_count} old shared post(s)")

    return cleaned


# ============================================
# FAILED-SHARE DEDUPE (with TTL)
# ============================================

def load_failed_shares() -> dict:
    """Load {normalized_url: failed_at_iso_timestamp} of recently-failed shares."""
    if not FAILED_SHARES_FILE.exists():
        return {}
    try:
        return json.loads(FAILED_SHARES_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"Could not load {FAILED_SHARES_FILE}: {e} — starting fresh")
        return {}


def save_failed_shares(state: dict) -> None:
    FAILED_SHARES_FILE.parent.mkdir(parents=True, exist_ok=True)
    FAILED_SHARES_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def prune_failed_shares(state: dict) -> dict:
    """Remove entries older than FAILED_SHARE_RETRY_HOURS so they get retried."""
    cutoff = datetime.now() - timedelta(hours=FAILED_SHARE_RETRY_HOURS)
    cutoff_iso = cutoff.isoformat()
    cleaned = {url: ts for url, ts in state.items() if ts > cutoff_iso}
    removed = len(state) - len(cleaned)
    if removed > 0:
        logger.info(f"Failed-shares: pruned {removed} entry/entries past TTL "
                    f"({FAILED_SHARE_RETRY_HOURS}h) — will retry")
    return cleaned


def mark_share_failed(state: dict, post_url: str) -> dict:
    """Record post_url as a recent failure so we don't retry until TTL elapses."""
    state[normalize_post_url(post_url)] = datetime.now().isoformat()
    save_failed_shares(state)
    return state


# ============================================
# PLAYWRIGHT SCRAPING - FACEBOOK POSTS
# ============================================

def normalize_post_url(url: str) -> str:
    """Normalize a Facebook post URL for dedup purposes.

    Strips FB tracking query params (__tn__, __cft__, ref, set, etc.)
    but PRESERVES identifier-bearing params that distinguish posts:
      - fbid       — photo identifier (/photo?fbid=XYZ)
      - v          — video identifier (/watch?v=XYZ)
      - story_fbid — story identifier (/permalink.php?story_fbid=XYZ)
      - id         — page id paired with story_fbid

    Previously this function did `url.split('?')[0]` which collapsed
    EVERY photo post to `https://www.facebook.com/photo` — making
    the first scraped photo "shared" for all eternity and blocking
    every subsequent photo post (Burmistrz, Kościół Zielonoświątkowy
    group photos, etc.) from being shared. Discovered 2026-06-22 when
    user reported that Burmistrz's posts visible on his profile were
    never appearing on bgnews.
    """
    if not url:
        return url
    from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
    try:
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        kept = {}
        for key in ('fbid', 'v', 'story_fbid', 'id'):
            if key in qs and qs[key]:
                kept[key] = qs[key][0]
        new_query = urlencode(kept) if kept else ''
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path,
                           parsed.params, new_query, ''))
    except Exception:
        # Defensive: if URL is malformed, fall back to the old behaviour
        return url.split('?')[0]


def parse_fb_posts(html: str, source_url: str, source_name: str) -> list:
    """Parse posts from Facebook page HTML.

    Extracts post permalinks (posts, videos, reels, photos, watch) and
    text snippets for logging.
    """
    soup = BeautifulSoup(html, 'html.parser')
    posts = []
    seen_urls = set()

    # Facebook wraps each post in a div with aria-posinset attribute
    post_divs = soup.find_all('div', attrs={'aria-posinset': True})

    for post_div in post_divs:
        # Extract post link - includes videos, reels, watch
        links = post_div.find_all(
            'a', href=re.compile(r'/posts/|/photo/|/videos/|/watch/|/reel/')
        )
        if not links:
            continue

        href = links[0].get('href', '')

        # CRITICAL: don't blindly strip query params. For /photo/ URLs the
        # fbid query is the photo identifier — strip it and you get a
        # generic "/photo/" URL that points nowhere and can't be shared.
        # Same risk for /video.php?v=... Keep query for these types.
        # For /posts/<id>/, /reel/<id>/, /watch/?v=<id> with id in path,
        # the trailing query is FB tracking junk and can be stripped.
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(href)
        path = parsed.path
        if '/photo/' in path or '/photo.php' in path:
            qs = parse_qs(parsed.query)
            fbid = (qs.get('fbid') or [''])[0]
            if fbid:
                post_url = f"https://www.facebook.com/photo?fbid={fbid}"
            else:
                # No fbid → this isn't shareable, skip rather than store junk
                continue
        elif '/video.php' in path or '/watch' in path:
            qs = parse_qs(parsed.query)
            vid = (qs.get('v') or [''])[0]
            if vid:
                post_url = f"https://www.facebook.com/watch/?v={vid}"
            else:
                continue
        else:
            # /posts/, /reel/, /videos/ — id is in path, strip tracking query
            clean_href = href.split('?')[0]
            if clean_href.startswith('/'):
                post_url = f"https://www.facebook.com{clean_href}"
            else:
                post_url = clean_href

        # Normalize for deduplication
        normalized = normalize_post_url(post_url)
        if normalized in seen_urls:
            continue
        seen_urls.add(normalized)

        # Extract text snippet for logging
        text_divs = post_div.find_all('div', attrs={'dir': 'auto'})
        text_snippet = ' '.join(d.get_text(strip=True) for d in text_divs[:3])
        if text_snippet:
            text_snippet = text_snippet[:200]

        posts.append({
            'url': post_url,
            'text_snippet': text_snippet,
            'source_name': source_name,
            'source_url': source_url,
            'scraped_at': datetime.now().isoformat(),
        })
        # Cap to avoid harvesting a long tail in a single scrape — keeps
        # share-burst bounded. Dedupe layer prevents older posts from
        # being missed permanently: they'll surface on subsequent runs
        # once the cap-cutoff ones get marked-shared.
        if len(posts) >= MAX_POSTS_PER_SOURCE:
            break

    return posts


async def scrape_fb_page(page_url: str, page_name: str) -> list:
    """Scrape recent posts from a single Facebook page using Playwright."""
    logger.info(f"Scraping: {page_name} ({page_url})")

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                )
            )
            page = await context.new_page()

            await page.goto(page_url, wait_until="networkidle", timeout=60000)
            await page.wait_for_timeout(3000)

            # Scroll generously to give FB time to lazy-load multiple posts
            # (more iterations + longer waits = more aria-posinset divs
            # rendered, up to MAX_POSTS_PER_SOURCE which the parser caps).
            for _ in range(6):
                await page.evaluate("window.scrollBy(0, 1000)")
                await page.wait_for_timeout(2000)

            html = await page.content()
            await browser.close()

        posts = parse_fb_posts(html, page_url, page_name)
        logger.info(f"  Found {len(posts)} post(s) from {page_name}")
        return posts

    except Exception as e:
        logger.error(f"  Error scraping {page_name}: {e}")
        return []


async def scrape_all_monitored_pages() -> list:
    """Scrape monitored Facebook pages via Playwright unauth.

    SKIPS sources flagged as _requires_auth_scrape (profile.php?id=…),
    those are scraped separately by scrape_all_auth_pages() once the
    Selenium driver is up — Playwright unauth gets blocked on them and
    returns 0 posts deterministically.
    """
    all_posts = []
    unauth_sources = [
        s for s in MONITORED_PAGES
        if not _requires_auth_scrape(s['url']) and not _source_is_blocked(s['url'])
    ]
    auth_sources_count = sum(
        1 for s in MONITORED_PAGES
        if _requires_auth_scrape(s['url']) and not _source_is_blocked(s['url'])
    )
    blocked_count = sum(1 for s in MONITORED_PAGES if _source_is_blocked(s['url']))
    if blocked_count:
        logger.info(f"🚫 {blocked_count} source(s) silently blocked by "
                    f"BLOCKED_SOURCE_URLS guard")

    for page_config in unauth_sources:
        posts = await scrape_fb_page(page_config['url'], page_config['name'])
        all_posts.extend(posts)

    logger.info(f"Total scraped (unauth): {len(all_posts)} post(s) from "
                f"{len(unauth_sources)} page(s); {auth_sources_count} auth-required "
                f"source(s) deferred to Selenium phase")
    return all_posts


# ============================================
# GROUP SCRAPING (authenticated, via Selenium)
# ============================================

def scrape_fb_group_with_selenium(driver, group_url: str, group_name: str) -> list:
    """Scrape recent posts from a Facebook GROUP using an already-authenticated
    Selenium driver. Required because Playwright unauth can't access group
    contents (groups require login regardless of public/closed setting).

    Reuses parse_fb_posts() — FB renders group post divs with the same
    aria-posinset attribute as page posts, so the parser doesn't need
    group-specific logic.
    """
    logger.info(f"Scraping group: {group_name} ({group_url})")
    try:
        # Prefer chronological sort so we see most recent first (the feed
        # default of "Activity" mixes in older posts with new comments).
        sep = "&" if "?" in group_url else "?"
        url = f"{group_url}{sep}sorting_setting=CHRONOLOGICAL"
        driver.get(url)
        time.sleep(4)
        # Scroll a bit to load posts past the fold
        for _ in range(3):
            driver.execute_script("window.scrollBy(0, 800)")
            time.sleep(1.5)
        html = driver.page_source
        posts = parse_fb_posts(html, group_url, group_name)
        logger.info(f"  Found {len(posts)} post(s) from {group_name}")
        return posts
    except Exception as e:
        logger.error(f"  Error scraping group {group_name}: {e}")
        return []


def scrape_fb_page_with_selenium(driver, page_url: str, page_name: str) -> list:
    """Scrape recent posts from a Facebook PAGE using the authenticated
    Selenium driver.

    Mirror of scrape_fb_group_with_selenium — same parse_fb_posts() logic
    (aria-posinset markers work for both groups, profile.php pages, and
    /handle/ pages). Used for sources where Playwright unauth gets
    blocked / returns 0 posts (typically profile.php?id=... — i.e. "new
    pages experience" profiles like Burmistrz Daniel Lubiński or Straż
    Miejska Boguszów-Gorce).
    """
    logger.info(f"Scraping (auth): {page_name} ({page_url})")
    try:
        driver.get(page_url)
        time.sleep(4)
        # Scroll generously — admin/auth view often lazy-loads even more
        # aggressively than unauth, but we need MAX_POSTS_PER_SOURCE rendered
        for _ in range(5):
            driver.execute_script("window.scrollBy(0, 900)")
            time.sleep(1.5)
        html = driver.page_source
        posts = parse_fb_posts(html, page_url, page_name)
        logger.info(f"  Found {len(posts)} post(s) from {page_name}")
        return posts
    except Exception as e:
        logger.error(f"  Error scraping page {page_name}: {e}")
        return []


def _source_is_blocked(source_url: str) -> bool:
    """Hard-block a source regardless of what MONITORED_PAGES contains.
    Enforces user-declared do-not-share list (Kościół Uliczny etc.).
    Called defensively before every scrape + before every share."""
    if not source_url:
        return False
    for token in BLOCKED_SOURCE_URLS:
        if token and token in source_url:
            return True
    return False


def scrape_all_auth_pages(driver) -> list:
    """Scrape all MONITORED_PAGES entries flagged as auth-required
    (profile.php?id=...) using the given Selenium driver. Sequential,
    with small human_delay between sources.

    BLOCKED_SOURCE_URLS entries are silently skipped even if they slip
    into MONITORED_PAGES (defense in depth against future config edits)."""
    auth_sources = [
        s for s in MONITORED_PAGES
        if _requires_auth_scrape(s['url']) and not _source_is_blocked(s['url'])
    ]
    blocked_count = sum(
        1 for s in MONITORED_PAGES
        if _requires_auth_scrape(s['url']) and _source_is_blocked(s['url'])
    )
    if blocked_count:
        logger.info(f"🚫 {blocked_count} auth source(s) silently blocked "
                    f"by BLOCKED_SOURCE_URLS guard")
    if not auth_sources:
        return []
    logger.info(f"--- Phase 2b: Auth scrape of {len(auth_sources)} profile.php source(s) ---")
    all_posts = []
    for cfg in auth_sources:
        posts = scrape_fb_page_with_selenium(driver, cfg['url'], cfg['name'])
        all_posts.extend(posts)
        human_delay(2, 4)
    return all_posts


def scrape_all_monitored_groups(driver) -> list:
    """Sequentially scrape all MONITORED_GROUPS using the given Selenium
    driver (must already be navigated/logged in as a member or admin).
    """
    all_posts = []
    for cfg in MONITORED_GROUPS:
        posts = scrape_fb_group_with_selenium(driver, cfg["url"], cfg["name"])
        all_posts.extend(posts)
        human_delay(2, 4)
    return all_posts


# ============================================
# SELENIUM SHARING FUNCTIONS
# ============================================

def setup_driver():
    """Setup Chrome driver using Docker Selenium."""
    if USE_DOCKER:
        from docker_selenium import get_docker_driver
        logger.info("Using Docker Selenium...")
        return get_docker_driver(max_retries=3)
    else:
        raise RuntimeError("Only Docker mode is supported for bg_fb_share.py")


def ensure_logged_in_as_page(driver):
    """Navigate to FB page and ensure we are logged in as the page.

    Uses 3-stage approach:
    - STAGE A: Check for immediate "Przelacz profil" modal popup
    - STAGE B: Look for sidebar "Przelacz teraz" button
    - STAGE C: Fallback - use top-right profile menu to switch
    """

    target_profile_name = "Boguszow-Gorce Newsy i Informacje"

    logger.info("Opening FB page to verify login...")
    driver.get(FB_PAGE_URL)
    human_delay(4, 6)

    # Check if login needed
    if len(driver.find_elements(By.NAME, "email")) > 0:
        logger.error("LOGIN PAGE DETECTED! Session may have expired.")
        logger.error("Please re-login via: python src/docker_fb_login.py")
        driver.save_screenshot(str(DEBUG_DIR / "debug_login_required.png"))
        return False

    # Handle cookie popup if present
    cookie_selectors = [
        "//button[contains(text(), 'Zezwol')]",
        "//button[contains(text(), 'Allow')]",
        "//button[contains(text(), 'Akceptuj')]",
        "//button[contains(text(), 'Accept')]",
        "//span[text()='Zezwol na wszystkie pliki cookie']",
        "[data-testid='cookie-policy-manage-dialog-accept-button']",
    ]

    for sel in cookie_selectors:
        try:
            if sel.startswith('['):
                cookie_btn = driver.find_element(By.CSS_SELECTOR, sel)
            else:
                cookie_btn = driver.find_element(By.XPATH, sel)
            cookie_btn.click()
            logger.info(f"Handled cookie popup: {sel}")
            human_delay(2, 3)
            break
        except Exception:
            pass

    logger.info(f"Ensuring we are switched to: {target_profile_name}")

    switched = False

    # ---------------------------------------------------------
    # STAGE A: Check for "Przelacz profil" MODAL (Pop-up)
    # ---------------------------------------------------------
    try:
        modal_switch_btn = WebDriverWait(driver, 4).until(
            EC.element_to_be_clickable((
                By.XPATH,
                "//div[@role='dialog']//span[text()='Prze\u0142\u0105cz']/ancestor::div[@role='button']"
            ))
        )
        if modal_switch_btn:
            logger.info("STAGE A: Found 'Przelacz' modal popup immediately.")
            modal_switch_btn.click()
            switched = True
            human_delay(3, 5)
    except Exception:
        logger.info("STAGE A: No immediate modal popup found.")

    # ---------------------------------------------------------
    # STAGE B: Check for Standard Sidebar "Przelacz teraz" Button
    # ---------------------------------------------------------
    if not switched:
        logger.info("STAGE B: Looking for sidebar 'Przelacz teraz' button...")
        switch_now_selectors = [
            "//span[text()='Prze\u0142\u0105cz teraz']",
            "//div[@role='button']//span[text()='Prze\u0142\u0105cz teraz']",
            "//div[contains(@class, 'x1i10hfl')]//span[text()='Prze\u0142\u0105cz teraz']",
        ]

        for selector in switch_now_selectors:
            try:
                btn = WebDriverWait(driver, 3).until(
                    EC.element_to_be_clickable((By.XPATH, selector))
                )
                if btn:
                    logger.info(f"STAGE B: Found sidebar button: {selector}")
                    driver.execute_script(
                        "arguments[0].scrollIntoView({block: 'center'});", btn
                    )
                    human_delay(0.5, 1)
                    btn.click()

                    # Handle the confirmation dialog
                    human_delay(1, 2)
                    confirm_selectors = [
                        "//div[@role='dialog']//span[text()='Prze\u0142\u0105cz']",
                        "//div[@role='dialog']//div[@role='button']//span[text()='Prze\u0142\u0105cz']",
                    ]
                    for c_sel in confirm_selectors:
                        try:
                            c_btn = WebDriverWait(driver, 3).until(
                                EC.element_to_be_clickable((By.XPATH, c_sel))
                            )
                            c_btn.click()
                            logger.info("STAGE B: Confirmed switch in dialog")
                            break
                        except Exception:
                            pass

                    switched = True
                    human_delay(3, 5)
                    break
            except Exception:
                continue

    # ---------------------------------------------------------
    # STAGE C: UNIVERSAL FALLBACK - Top-Right Menu
    # ---------------------------------------------------------
    if not switched:
        logger.info("STAGE B failed. Executing STAGE C: Top-Right Menu Switch strategy.")

        menu_opened = False

        account_menu_selectors = [
            "//div[@role='button'][@aria-label='Tw\u00f3j profil']",
            "//div[@aria-label='Tw\u00f3j profil']",
            "//svg[@aria-label='Tw\u00f3j profil']/ancestor::div[@role='button']",
            "//div[@aria-label='Mechanizmy kontrolne i ustawienia konta']//div[@role='button']",
            "//div[@aria-label='Your profile']",
            "//div[@aria-label='Account controls and settings']//div[@role='button']",
            "//div[@role='navigation']//div[@role='button']//image",
            "//div[@role='banner']//div[@role='button'][.//image]",
        ]

        # Attempt 1: Standard Selectors with JavaScript Click
        for sel in account_menu_selectors:
            try:
                menu_btn = WebDriverWait(driver, 3).until(
                    EC.presence_of_element_located((By.XPATH, sel))
                )
                logger.info(f"STAGE C: Found menu button: {sel}")
                driver.execute_script("arguments[0].click();", menu_btn)
                menu_opened = True
                human_delay(2, 3)
                break
            except Exception:
                continue

        # Attempt 2: Coordinate Click (Force) if selectors fail
        if not menu_opened:
            logger.warning("STAGE C: Selectors failed. Clicking Top-Right coordinates...")
            try:
                action = ActionChains(driver)
                action.move_by_offset(1860, 45).click().perform()
                action.move_by_offset(-1860, -45).perform()
                logger.info("STAGE C: Clicked coordinates (1860, 45)")
                menu_opened = True
                human_delay(2, 3)
            except Exception as e:
                logger.error(f"STAGE C: Coordinate click failed: {e}")

        # If menu is open, find the target profile
        if menu_opened:
            try:
                target_xpath = f"//span[contains(text(), '{target_profile_name}')]"

                target_profile = WebDriverWait(driver, 5).until(
                    EC.element_to_be_clickable((By.XPATH, target_xpath))
                )
                target_profile.click()
                logger.info(f"STAGE C: Clicked target profile '{target_profile_name}'")
                switched = True
                human_delay(5, 7)

            except Exception:
                logger.warning("Target not visible immediately. Trying 'Zobacz wszystkie profile'...")
                try:
                    see_all_selectors = [
                        "//span[contains(text(), 'Zobacz wszystkie profile')]",
                        "//span[contains(text(), 'See all profiles')]",
                    ]

                    for see_sel in see_all_selectors:
                        try:
                            see_all = driver.find_element(By.XPATH, see_sel)
                            see_all.click()
                            human_delay(2, 3)
                            break
                        except Exception:
                            continue

                    # Now try finding the name again
                    target_profile = WebDriverWait(driver, 5).until(
                        EC.element_to_be_clickable((By.XPATH, target_xpath))
                    )
                    target_profile.click()
                    logger.info(f"STAGE C: Clicked target profile after expanding list")
                    switched = True
                    human_delay(5, 7)
                except Exception as e:
                    logger.error(f"STAGE C failed to find profile in menu: {e}")
                    driver.save_screenshot(str(DEBUG_DIR / "debug_stage_c_fail.png"))
        else:
            logger.error("STAGE C: Could not open menu.")

    if not switched:
        logger.warning("Could not verify profile switch. Attempting to proceed (maybe already correct?)...")
    else:
        logger.info("Profile switch logic completed.")
        human_delay(3, 4)

    return True


_PL_DIACRITIC_MAP = str.maketrans(
    'ąćęłńóśźżĄĆĘŁŃÓŚŹŻ',
    'acelnoszzACELNOSZZ',
)


def _strip_pl(s: str) -> str:
    """Diacritic-insensitive comparison helper. FB displays page names
    with proper Polish diacritics ('Boguszów-Gorce', 'Straż Miejska')
    but our MONITORED_PAGES source_name fields are typed without diacritics
    ('Boguszow-Gorce', 'Straz Miejska'). Strip both sides before substring
    match to avoid a false miss."""
    return (s or '').translate(_PL_DIACRITIC_MAP)


def _click_robust(driver, el):
    """Click an element, falling back to JS click if the native click
    is intercepted (e.g. FB renders an <img> on top of the share button
    region). Scrolls into view first to maximise the chance of a clean
    native click."""
    try:
        driver.execute_script(
            "arguments[0].scrollIntoView({block: 'center', inline: 'center'});", el
        )
        time.sleep(0.4)
    except Exception:
        pass
    try:
        el.click()
        return True
    except Exception as e_native:
        logger.info(f"  native click intercepted ({type(e_native).__name__}); falling back to JS click")
    try:
        driver.execute_script("arguments[0].click();", el)
        return True
    except Exception as e_js:
        logger.error(f"  JS click also failed: {e_js}")
        return False


def _find_share_button_for_post(driver, post: dict, max_wait_s: int = 15):
    """Locate the Share button for the SPECIFIC post we navigated to.

    Real FB share buttons have aria-label starting with 'Udostępnij post '
    (or 'Share post ') followed by the page name. A permalink page
    often renders multiple posts (the target post + suggestions + other
    pages' reshares of the same content), each with its own share button.
    Topmost-by-y is unreliable: in the 2026-06-30 diagnosis FB rendered
    Zespół Szkolno-Przedszkolny's reshare of a Gmina post ABOVE the Gmina
    original on the permalink page, so topmost would have shared the wrong
    page's post.

    Resolution order:
      1. precise — proper-button aria-label diacritic-insensitively contains
         a distinctive source-name token (e.g. 'Gmina', 'Burmistrz', 'Stra')
      2. fallback — proper-button aria-label diacritic-insensitively contains
         the FULL stripped source_name as a substring
      3. last resort — topmost proper-button (warn loud)
      4. Reel / generic aria-label='Udostępnij' button
      5. legacy 'Send this to friends...' aria-label

    NO `<span>` text fallback — that was the original bug. Spans nested
    inside the button bubble clicks up to the parent, but FB renders
    span-text labels in DOM ORDER not matching visual order, so the
    span fallback frequently selected the wrong post's button.
    """
    source_name = post.get('source_name', '') or ''
    source_stripped = _strip_pl(source_name)
    # Most distinctive tokens for our MONITORED_PAGES (first-word usually
    # works: 'Gmina', 'Burmistrz', 'Straz', 'OSP', 'MBPCK', 'Zespol',
    # 'OSiR', 'Gornik', 'HEROS', 'Stajnia', 'Stodola', 'Osrodek', 'Kosciol'.
    # 'Boguszow-Gorce' appears in many so we use the FIRST distinctive
    # word as the precise needle, falling back to full name match.)
    distinctive_tokens = [w for w in source_stripped.split() if len(w) >= 4]

    deadline = time.time() + max_wait_s
    while time.time() < deadline:
        # Collect ALL proper share buttons currently on the page
        try:
            xpath = (
                "//div[@role='button' and "
                "(starts-with(@aria-label, 'Udost\u0119pnij post ') or "
                " starts-with(@aria-label, 'Share post '))]"
            )
            candidates = driver.find_elements(By.XPATH, xpath)
        except Exception:
            candidates = []

        visible_buttons = []
        for c in candidates:
            try:
                if not c.is_displayed():
                    continue
                label = c.get_attribute('aria-label') or ''
                visible_buttons.append((c, label, _strip_pl(label), c.rect.get('y', 0)))
            except Exception:
                continue

        # (1) precise — token match on stripped aria-label
        for el, label, stripped, y in visible_buttons:
            for tok in distinctive_tokens:
                if tok in stripped:
                    logger.info(f"  Found Share button (precise '{tok}'): {label!r}")
                    return el

        # (2) fallback — full source name as substring (stripped both sides)
        if source_stripped:
            for el, label, stripped, y in visible_buttons:
                if source_stripped in stripped:
                    logger.info(f"  Found Share button (fullname): {label!r}")
                    return el

        # (3) topmost — last resort, log a warning so we notice
        if visible_buttons:
            visible_buttons.sort(key=lambda t: t[3])
            el, label, stripped, y = visible_buttons[0]
            logger.warning(
                f"  ⚠️ Source name '{source_name}' not found in any share-button aria-label; "
                f"falling back to topmost (y={y:.0f}): {label!r} — risk of sharing wrong post"
            )
            return el

        # (4) Reel / simpler aria-label
        try:
            xpath = (
                "//div[@role='button' and "
                "(@aria-label='Udost\u0119pnij' or @aria-label='Share')]"
            )
            reel_candidates = driver.find_elements(By.XPATH, xpath)
            visible = [(c, c.rect.get('y', 0)) for c in reel_candidates if c.is_displayed()]
            if visible:
                visible.sort(key=lambda kv: kv[1])
                el = visible[0][0]
                logger.info(f"  Found Share button (Reel-style, y={visible[0][1]:.0f})")
                return el
        except Exception:
            pass

        # (5) legacy aria-labels
        try:
            xpath = (
                "//div[@aria-label='Send this to friends or post it on your profile.' "
                "or @aria-label='Wy\u015blij znajomym lub opublikuj na swoim profilu.']"
            )
            el = driver.find_element(By.XPATH, xpath)
            if el.is_displayed():
                logger.info("  Found Share button (legacy aria-label)")
                return el
        except Exception:
            pass

        time.sleep(0.5)

    return None


def _extract_post_identifier(url: str) -> str:
    """Extract a unique-enough identifier from a source post URL — used
    as a DOM needle to grep our own wall after a share, to confirm the
    embed actually landed. Returns None if no identifier is recognisable.

    Match priority (most specific first):
      /posts/pfbid<hash>     → 'pfbid<hash>'
      /photo?fbid=<digits>   → '<digits>'
      /reel/<digits>         → '<digits>'
      /watch?v=<digits>      → '<digits>'
      /videos/<digits>       → '<digits>'
    """
    if not url:
        return None
    m = re.search(r'pfbid[A-Za-z0-9_-]+', url)
    if m:
        return m.group(0)
    m = re.search(r'[?&]fbid=(\d+)', url)
    if m:
        return m.group(1)
    m = re.search(r'/reel/(\d+)', url)
    if m:
        return m.group(1)
    m = re.search(r'[?&]v=(\d+)', url)
    if m:
        return m.group(1)
    m = re.search(r'/videos/(\d+)', url)
    if m:
        return m.group(1)
    return None


def verify_share_succeeded(driver, post: dict, scroll_rounds: int = 4) -> bool:
    """Navigate to bgnews own wall and confirm the just-shared post is
    actually visible there. Closes the "No confirmation toast after 30s,
    assuming share completed" false-positive loophole.

    Background — diagnosis 2026-06-30:
      live wall scrape of bgnews showed that 7 of 8 "Successfully shared"
      posts from the 11:30 cron run were NOT actually on the wall. The
      script clicks "Udostępnij w Aktualnościach", waits 30s for a toast
      that almost never appears (645/655 = 98.5% of "successes" log
      "No confirmation toast"), then assumes success and writes to
      shared_posts.json — permanently blocking that URL from retry.

    Detection strategy:
      Extract a unique identifier from the source URL (pfbid hash or
      photo fbid). Navigate to FB_PAGE_URL (our own wall). Scroll a
      few times so the top of feed renders. Search the rendered HTML
      for the identifier — if found, the embed is on our wall.

    Failure modes & policy:
      - identifier not extractable    → return True (fail-open: rare,
        no need to penalise an unusual URL format we can't probe)
      - navigation / scrape exception → return False (fail-closed:
        if verification couldn't run, the share probably hit similar
        FB-side trouble; retry is the safer default)
      - identifier extractable but not in DOM → return False
    """
    identifier = _extract_post_identifier(post['url'])
    if not identifier:
        logger.info(f"  ⚠️ Verify skipped — no extractable identifier in {post['url']}")
        return True

    needle = identifier
    logger.info(f"  🔎 Verifying share on bgnews wall — needle: '{needle}'")
    try:
        driver.get(FB_PAGE_URL)
        time.sleep(5)
        for _ in range(scroll_rounds):
            driver.execute_script("window.scrollBy(0, 1000)")
            time.sleep(1.5)
        html = driver.page_source
        if needle in html:
            logger.info(f"  ✅ Verified: needle '{needle}' present on bgnews wall")
            return True
        logger.warning(f"  ❌ Verify FAILED: needle '{needle}' NOT on bgnews wall — share did not land")
        try:
            ts = int(time.time())
            driver.save_screenshot(str(DEBUG_DIR / f"debug_verify_fail_{ts}.png"))
        except Exception:
            pass
        return False
    except Exception as e:
        logger.error(f"  ⚠️ Verify exception (fail-closed): {e}")
        return False


def share_post(driver, post: dict) -> bool:
    """Share a single Facebook post to our page's feed.

    This navigates to the original post, clicks Share, then clicks
    "Share now (Public)" / "Udostepnij teraz (Publiczne)".
    No additional text is added -- just a clean share.

    Args:
        driver: Selenium WebDriver instance
        post: Post dict with 'url', 'text_snippet', 'source_name'

    Returns:
        True if shared AND verified on our wall, False otherwise.
        Verification (via verify_share_succeeded) closes the
        "No confirmation toast → assume success" loophole.
    """
    post_url = post['url']
    source_name = post['source_name']
    text_preview = (post.get('text_snippet') or '')[:80]

    logger.info(f"Sharing post from {source_name}: {post_url}")
    if text_preview:
        logger.info(f"  Preview: {text_preview}...")

    try:
        # Step 1: Navigate to the original post
        logger.info(f"  Navigating to post URL...")
        driver.get(post_url)
        human_delay(4, 6)

        # Handle any login redirect
        if len(driver.find_elements(By.NAME, "email")) > 0:
            logger.error("  Login page detected! Cannot share.")
            driver.save_screenshot(str(DEBUG_DIR / "debug_share_login_required.png"))
            return False

        # Step 2: Find and click the "Share" / "Udostępnij" button on the post.
        #
        # Bug history (2026-06-30): the prior selector list ended with
        # //span[text()='Udostępnij'] as a fallback. Inspection of a real
        # FB post page (gminamiastoboguszowgorce/posts/pfbid…) showed
        # FOUR matches for "Udostępnij" — two proper role=button divs
        # (one for the main post we navigated to, one for a SUGGESTED
        # post FB renders alongside) and two label spans nested inside
        # each button. The span selector won the race in 669/669 = 100%
        # of attempts because it matched IMMEDIATELY (no aria-label
        # filtering) — and worst, it sometimes matched the SUGGESTED
        # post's span first in DOM order. So we silently clicked the
        # share button for SOME OTHER post (Koleje, etc.), then
        # "Udostępnij w Aktualnościach" shared that random post — and
        # verify for our intended pfbid hash on the wall correctly
        # failed. Net effect: we were sharing the wrong posts (silently)
        # and verify (added earlier today) caught it as zero-landing.
        #
        # Real share buttons:
        #   <div role='button' aria-label='Udostępnij post <Page Name>'>
        # The aria-label STARTS WITH 'Udostępnij post ' (note the space).
        # We collect ALL matches and pick the one whose label contains
        # the source page name; failing that, the TOPMOST one (smallest
        # y) which is reliably the main post on a permalink page.
        is_reel = '/reel/' in post_url
        if is_reel:
            logger.info("  Reel detected — Reel share button has shorter aria-label")
        logger.info("  Looking for Share button...")

        share_btn = _find_share_button_for_post(driver, post)

        if not share_btn:
            logger.error("  Could not find Share button on the post")
            driver.save_screenshot(str(DEBUG_DIR / f"debug_no_share_btn_{int(time.time())}.png"))
            return False

        if not _click_robust(driver, share_btn):
            logger.error("  Could not click Share button at all")
            return False
        logger.info("  Clicked Share button")
        human_delay(2, 3)

        # Step 3: In the share menu, click "Share now (Public)" or "Share to Feed"
        logger.info("  Looking for 'Share now' / 'Udostepnij teraz' option...")

        share_now_selectors = [
            "//span[text()='Udost\u0119pnij teraz (publiczne)']",
            "//span[text()='Udost\u0119pnij teraz (Publiczne)']",
            "//span[text()='Share now (Public)']",
            "//span[contains(text(), 'Udost\u0119pnij teraz')]",
            "//span[contains(text(), 'Share now')]",
            # Fallback: "Share to Feed"
            "//span[text()='Udost\u0119pnij w aktualno\u015bciach']",
            "//span[text()='Udost\u0119pnij w Aktualno\u015bciach']",
            "//span[contains(text(), 'w aktualno\u015bci')]",
            "//span[contains(text(), 'w Aktualno\u015bci')]",
            "//span[text()='Share to Feed']",
        ]

        share_now_btn = None
        for selector in share_now_selectors:
            try:
                share_now_btn = WebDriverWait(driver, 5).until(
                    EC.element_to_be_clickable((By.XPATH, selector))
                )
                if share_now_btn:
                    logger.info(f"  Found 'Share now' option: {selector}")
                    break
            except Exception:
                continue

        if not share_now_btn:
            # Fallback: Try clicking via role=menuitem
            logger.warning("  Standard selectors failed. Trying menuitem fallback...")
            try:
                menu_items = driver.find_elements(
                    By.XPATH, "//div[@role='menuitem'] | //div[@role='menu']//div[@role='button']"
                )
                for item in menu_items:
                    item_text = item.text.lower()
                    if any(kw in item_text for kw in [
                        'udost\u0119pnij teraz', 'share now',
                        'w aktualno\u015bci', 'share to feed'
                    ]):
                        share_now_btn = item
                        logger.info(f"  Found via menuitem: '{item.text.strip()[:60]}'")
                        break
            except Exception:
                pass

        if not share_now_btn:
            logger.error("  Could not find 'Share now' or 'Share to Feed' option")
            driver.save_screenshot(
                str(DEBUG_DIR / f"debug_no_share_now_{int(time.time())}.png")
            )
            return False

        # Click "Share now" / "Udostępnij w Aktualnościach". This is a
        # SPAN nested inside a role=button div with no aria-label — so
        # click on the span bubbles up to the parent button. Walk to
        # the parent button explicitly to get a more reliable click.
        try:
            parent_btn = share_now_btn.find_element(
                By.XPATH, "ancestor::div[@role='button'][1]"
            )
            _click_robust(driver, parent_btn)
        except Exception:
            _click_robust(driver, share_now_btn)
        logger.info("  Clicked 'Share now'")
        human_delay(3, 5)

        # Step 4: Handle the "Napisz post" composer that FB now opens.
        # Bug discovered 2026-06-30: clicking "Udostępnij w Aktualnościach"
        # does NOT publish — it opens a composer dialog where you could
        # add a caption above the embedded share, and at the bottom is
        # a blue "Dalej" button. Without that final "Dalej" click, the
        # composer stays open forever and the share never lands. Older
        # bg_fb_share.py code just waited 30s for a toast (never came),
        # logged "assuming share completed", and silently false-positived.
        #
        # Sequence we now perform:
        #   a) wait for the 'Napisz post' / 'Create post' dialog to appear
        #   b) click the "Dalej" / "Next" button → submits the share
        #   c) wait for the dialog to close (success signal)
        #   d) handle any post-publish upsell popup ("Nie teraz" etc.)
        logger.info("  Waiting for 'Napisz post' composer to open...")
        composer = None
        deadline = time.time() + 12
        while time.time() < deadline:
            try:
                composer = driver.find_element(
                    By.XPATH,
                    "//div[@role='dialog' and (@aria-label='Napisz post' "
                    "or @aria-label='Create post' or @aria-label='Create Post')]"
                )
                if composer.is_displayed():
                    break
            except Exception:
                pass
            time.sleep(0.5)

        if composer:
            logger.info("  Composer opened — polling for submit button (aria-label match)")
            # FB renders the submit button as <div role='button' aria-label='Dalej'>
            # 5 levels deep inside the dialog (verified via DOM inspection
            # 2026-06-30). Search by aria-label directly to avoid brittle
            # nested-span paths. Poll because FB defers button rendering
            # by 1-3s after the dialog mount.
            submit_clicked = False
            submit_deadline = time.time() + 12
            submit_xpaths = [
                "//div[@role='dialog']//div[@role='button' and @aria-label='Dalej']",
                "//div[@role='dialog']//div[@role='button' and @aria-label='Next']",
                "//div[@role='dialog']//div[@role='button' and @aria-label='Opublikuj']",
                "//div[@role='dialog']//div[@role='button' and @aria-label='Post']",
                "//div[@role='dialog']//div[@role='button' and @aria-label='Udost\u0119pnij']",
                "//div[@role='dialog']//div[@role='button' and @aria-label='Share']",
            ]
            while time.time() < submit_deadline and not submit_clicked:
                for xp in submit_xpaths:
                    try:
                        elements = driver.find_elements(By.XPATH, xp)
                        for el in elements:
                            if not el.is_displayed():
                                continue
                            logger.info(f"  Found composer submit: aria-label="
                                        f"{el.get_attribute('aria-label')!r}")
                            if not _click_robust(driver, el):
                                continue
                            submit_clicked = True
                            break
                        if submit_clicked:
                            break
                    except Exception:
                        continue
                if not submit_clicked:
                    time.sleep(0.5)

            if not submit_clicked:
                logger.error("  Composer open but NO submit button found within 12s — aborting")
                try:
                    driver.save_screenshot(
                        str(DEBUG_DIR / f"debug_composer_stuck_{int(time.time())}.png")
                    )
                except Exception:
                    pass
                return False

            # 4b: After "Dalej" the dialog title flips to "Ustawienia posta"
            # (Post Settings). The final BLUE submit button (aria-label
            # exactly 'Udostępnij' or 'Share', not 'Udostępnij post X')
            # lives in a sibling DOM portal — Selenium does NOT find it
            # via `dialog//button` scoped search. Match by exact aria-label
            # at document scope to discriminate from page-specific share
            # buttons that share the same word.
            logger.info("  Waiting for Ustawienia posta dialog + blue final submit...")
            final_clicked = False
            final_deadline = time.time() + 15
            while time.time() < final_deadline and not final_clicked:
                for final_xpath in [
                    "//div[@role='button' and @aria-label='Udost\u0119pnij']",
                    "//div[@role='button' and @aria-label='Share']",
                    "//div[@role='button' and @aria-label='Opublikuj']",
                    "//div[@role='button' and @aria-label='Post']",
                ]:
                    try:
                        elements = driver.find_elements(By.XPATH, final_xpath)
                        visible = [e for e in elements if e.is_displayed()]
                        if not visible:
                            continue
                        # Multiple matches possible (rare). Pick the
                        # bottom-most (largest y) — final submit is always
                        # at the dialog footer.
                        visible_with_y = [(e, e.rect.get('y', 0)) for e in visible]
                        visible_with_y.sort(key=lambda kv: kv[1], reverse=True)
                        el = visible_with_y[0][0]
                        logger.info(f"  Found final submit: aria-label="
                                    f"{el.get_attribute('aria-label')!r} "
                                    f"y={visible_with_y[0][1]:.0f}")
                        if not _click_robust(driver, el):
                            continue
                        final_clicked = True
                        break
                    except Exception:
                        continue
                if not final_clicked:
                    time.sleep(0.5)

            if not final_clicked:
                logger.error("  Final submit button never appeared — share aborted")
                try:
                    driver.save_screenshot(
                        str(DEBUG_DIR / f"debug_no_final_submit_{int(time.time())}.png")
                    )
                except Exception:
                    pass
                return False

            # 4c: Wait for the composer dialog to actually close as the
            # confirmation signal. If it closes within ~25s → share landed.
            logger.info("  Waiting for composer to close...")
            close_deadline = time.time() + 25
            while time.time() < close_deadline:
                try:
                    still_open = composer.is_displayed()
                    if not still_open:
                        logger.info("  ✅ Composer closed — share submitted")
                        break
                except Exception:
                    # Stale ref usually means dialog DOM removed → closed
                    logger.info("  ✅ Composer DOM gone — share submitted")
                    break
                time.sleep(0.5)
            else:
                logger.warning("  ⚠️ Composer still open after 25s — share may have failed")
        else:
            # No composer appeared — could mean FB published immediately
            # (rare, older UI). Don't fail here — let verify_share_succeeded
            # be the ground truth.
            logger.info("  No composer appeared within 12s — proceeding to verify")

        # Step 5: Handle any popups
        popup_selectors = [
            "//span[text()='Nie teraz']",
            "//div[@role='button']//span[text()='Nie teraz']",
            "//span[text()='Not Now']",
            "//span[text()='Pomi\u0144']",
            "//span[text()='Skip']",
        ]

        for sel in popup_selectors:
            try:
                popup_btn = WebDriverWait(driver, 2).until(
                    EC.element_to_be_clickable((By.XPATH, sel))
                )
                popup_btn.click()
                logger.info(f"  Dismissed popup: {sel}")
                human_delay(1, 2)
                break
            except Exception:
                continue

        logger.info(f"  Clicked-through complete from {source_name} — running post-share verification")

        # Critical: FB's share dialog often closes without raising any
        # error even when the share itself was silently rejected (no
        # confirmation toast, no exception). Confirm by checking our own
        # wall for the source post's identifier. See verify_share_succeeded
        # docstring for the 2026-06-30 diagnosis.
        if not verify_share_succeeded(driver, post):
            logger.warning(f"  ❌ Share from {source_name} did NOT verify — treating as failure")
            return False

        logger.info(f"  ✅ Successfully shared & verified post from {source_name}")
        return True

    except Exception as e:
        logger.error(f"  Error sharing post: {e}")
        logger.error(traceback.format_exc())
        try:
            driver.save_screenshot(
                str(DEBUG_DIR / f"debug_share_error_{int(time.time())}.png")
            )
        except Exception:
            pass
        return False


# ============================================
# MAIN
# ============================================

def main():
    logger.info("=" * 60)
    logger.info(f"bg_fb_share.py START - TestMode={TEST_MODE}")
    logger.info("=" * 60)

    # Step 1: Acquire script lock
    if not acquire_script_lock():
        logger.error("Another instance is already running. Exiting.")
        sys.exit(1)

    # Register cleanup handlers
    atexit.register(release_script_lock)

    # Step 2: Load shared_posts.json + failed_shares.json, cleanup
    shared_posts = load_shared_posts()
    shared_posts = cleanup_old_posts(shared_posts)
    save_shared_posts(shared_posts)
    failed_shares = load_failed_shares()
    failed_shares = prune_failed_shares(failed_shares)
    save_failed_shares(failed_shares)

    # Step 3: Scrape all monitored pages for recent posts using Playwright
    logger.info("--- Phase 1: Scraping monitored pages (Playwright unauth) ---")
    page_posts = asyncio.run(scrape_all_monitored_pages())

    # Filter pages-source posts by what's already shared OR recently failed
    new_page_posts = []
    failed_skipped = 0
    for p in page_posts:
        nu = normalize_post_url(p['url'])
        if nu in shared_posts:
            continue
        if nu in failed_shares:
            failed_skipped += 1
            continue
        new_page_posts.append(p)
    if failed_skipped > 0:
        logger.info(f"Pages: skipped {failed_skipped} post(s) that failed recently "
                    f"(will retry after {FAILED_SHARE_RETRY_HOURS}h)")
    page_skipped = len(page_posts) - len(new_page_posts)
    if page_skipped > 0:
        logger.info(f"Pages: skipped {page_skipped} already-shared post(s)")

    # Early exit only if NO unauth page posts AND no groups AND no
    # auth-required pages — i.e. truly nothing to do that requires
    # spinning up Selenium.
    has_auth_pages = any(_requires_auth_scrape(s['url']) for s in MONITORED_PAGES)
    if not new_page_posts and not MONITORED_GROUPS and not has_auth_pages:
        logger.info("No new posts to share and no groups/auth pages configured.")
        return

    if TEST_MODE and not MONITORED_GROUPS:
        # Pure dry-run path when no groups would be touched
        logger.info(f"[TEST MODE] {len(new_page_posts)} new page post(s) would be shared:")
        for i, p in enumerate(new_page_posts, 1):
            logger.info(f"  {i}. [{p['source_name']}] {p['url']}")
        return

    # Step 4: Launch Docker Selenium (needed for groups AND for sharing)
    logger.info("--- Phase 2: Sharing via Selenium ---")
    driver = None

    try:
        driver = setup_driver()

        # Step 5: Ensure logged in as the page
        if not ensure_logged_in_as_page(driver):
            logger.error("Could not verify page login. Aborting.")
            return

        # Step 6: Authenticated scrape of profile.php pages (Burmistrz,
        # Straż Miejska, etc.) — Playwright unauth couldn't see them
        new_posts = list(new_page_posts)
        auth_page_posts = scrape_all_auth_pages(driver)
        new_auth_posts = []
        auth_already_skipped = 0
        auth_failed_skipped = 0
        for p in auth_page_posts:
            nu = normalize_post_url(p['url'])
            if nu in shared_posts:
                auth_already_skipped += 1
                continue
            if nu in failed_shares:
                auth_failed_skipped += 1
                continue
            new_auth_posts.append(p)
        if auth_already_skipped > 0:
            logger.info(f"Auth pages: skipped {auth_already_skipped} already-shared post(s)")
        if auth_failed_skipped > 0:
            logger.info(f"Auth pages: skipped {auth_failed_skipped} post(s) failed recently")
        new_posts.extend(new_auth_posts)

        # Step 6b: Authenticated group scrape (uses same driver)
        if MONITORED_GROUPS:
            logger.info(f"--- Phase 2a: Scraping {len(MONITORED_GROUPS)} group(s) (authenticated) ---")
            group_posts = scrape_all_monitored_groups(driver)
            new_group_posts = []
            group_already_skipped = 0
            group_failed_skipped = 0
            for p in group_posts:
                nu = normalize_post_url(p['url'])
                if nu in shared_posts:
                    group_already_skipped += 1
                    continue
                if nu in failed_shares:
                    group_failed_skipped += 1
                    continue
                new_group_posts.append(p)
            if group_already_skipped > 0:
                logger.info(f"Groups: skipped {group_already_skipped} already-shared post(s)")
            if group_failed_skipped > 0:
                logger.info(f"Groups: skipped {group_failed_skipped} post(s) failed recently")
            new_posts.extend(new_group_posts)

        if not new_posts:
            logger.info("No new posts to share (after group scrape).")
            return

        logger.info(f"Found {len(new_posts)} new post(s) to share:")
        for i, post in enumerate(new_posts, 1):
            logger.info(f"  {i}. [{post['source_name']}] {post['url']}")
            if post.get('text_snippet'):
                logger.info(f"     {post['text_snippet'][:100]}...")

        if TEST_MODE:
            logger.info("[TEST MODE] Would share the above posts. Exiting.")
            return

        # Step 7: Share each new post
        shared_count = 0

        for i, post in enumerate(new_posts):
            logger.info(f"--- Sharing {i+1}/{len(new_posts)} ---")

            success = share_post(driver, post)

            if success:
                # Mark as shared immediately
                normalized_url = normalize_post_url(post['url'])
                shared_posts[normalized_url] = datetime.now().isoformat()
                save_shared_posts(shared_posts)
                shared_count += 1
                logger.info(f"  Marked as shared: {normalized_url}")
            else:
                logger.warning(f"  Failed to share: {post['url']}")
                # Add to failed_shares so we don't retry every 4h. TTL via
                # FAILED_SHARE_RETRY_HOURS allows automatic re-attempt if
                # FB UI changes or we ship a selector fix.
                failed_shares = mark_share_failed(failed_shares, post['url'])
                logger.info(f"  Marked failed (retry after {FAILED_SHARE_RETRY_HOURS}h)")

            # Random delay between shares (except after the last one)
            if i < len(new_posts) - 1:
                delay = random.randint(MIN_DELAY_BETWEEN_SHARES, MAX_DELAY_BETWEEN_SHARES)
                logger.info(f"  Waiting {delay}s before next share...")
                time.sleep(delay)

        logger.info(f"Done! Shared {shared_count}/{len(new_posts)} post(s)")

    except Exception as e:
        logger.error(f"Critical error: {e}")
        logger.error(traceback.format_exc())
    finally:
        if driver:
            human_delay(2, 3)
            logger.info("Closing browser...")
            driver.quit()

    logger.info("=" * 60)
    logger.info("bg_fb_share.py FINISHED")
    logger.info("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)
