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
    {"name": "Kosciol Uliczny Boguszow-Gorce",     "url": "https://www.facebook.com/profile.php?id=100067837419514"},
    # --- TODO: WEBSITE sources (non-FB) handled by bg_scraper_selenium.py
    #     extension. Pending: https://bip.boguszow-gorce.pl/ (BIP)
]

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

DAYS_TO_KEEP = 7

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

            # Scroll down to load more posts
            for _ in range(3):
                await page.evaluate("window.scrollBy(0, 800)")
                await page.wait_for_timeout(1500)

            html = await page.content()
            await browser.close()

        posts = parse_fb_posts(html, page_url, page_name)
        logger.info(f"  Found {len(posts)} post(s) from {page_name}")
        return posts

    except Exception as e:
        logger.error(f"  Error scraping {page_name}: {e}")
        return []


async def scrape_all_monitored_pages() -> list:
    """Scrape all monitored Facebook pages for recent posts."""
    all_posts = []

    for page_config in MONITORED_PAGES:
        posts = await scrape_fb_page(page_config['url'], page_config['name'])
        all_posts.extend(posts)

    logger.info(f"Total scraped: {len(all_posts)} post(s) from {len(MONITORED_PAGES)} page(s)")
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


def share_post(driver, post: dict) -> bool:
    """Share a single Facebook post to our page's feed.

    This navigates to the original post, clicks Share, then clicks
    "Share now (Public)" / "Udostepnij teraz (Publiczne)".
    No additional text is added -- just a clean share.

    Args:
        driver: Selenium WebDriver instance
        post: Post dict with 'url', 'text_snippet', 'source_name'

    Returns:
        True if shared successfully, False otherwise
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

        # Step 2: Find and click the "Share" / "Udostepnij" button on the post
        # Different post types render different share-button aria-labels:
        #   - Regular post: aria-label='Send this to friends or post it on your profile.'
        #   - Reel: aria-label='Udostępnij' (the simpler one)
        #   - Photo: similar to regular post
        # Order matters — try Reel-specific aria-label early so a Reel doesn't
        # accidentally match a sidebar "Udostępnij" span first.
        is_reel = '/reel/' in post_url
        if is_reel:
            logger.info("  Reel detected — using Reel-specific share selectors")
        logger.info("  Looking for Share button...")

        share_button_selectors = [
            "//div[@aria-label='Send this to friends or post it on your profile.']",
            "//div[@aria-label='Wy\u015blij znajomym lub opublikuj na swoim profilu.']",
            # Reel uses a simpler aria-label — also generic 'Udostępnij' on photo posts
            "//div[@aria-label='Udost\u0119pnij' and @role='button']",
            "//div[@aria-label='Share' and @role='button']",
            "//span[text()='Udost\u0119pnij']",
            "//span[text()='Share']",
        ]

        share_btn = None
        for selector in share_button_selectors:
            try:
                share_btn = WebDriverWait(driver, 8).until(
                    EC.element_to_be_clickable((By.XPATH, selector))
                )
                if share_btn:
                    logger.info(f"  Found Share button: {selector}")
                    break
            except Exception:
                continue

        if not share_btn:
            logger.error("  Could not find Share button on the post")
            driver.save_screenshot(str(DEBUG_DIR / f"debug_no_share_btn_{int(time.time())}.png"))
            return False

        # Scroll to share button and click
        driver.execute_script(
            "arguments[0].scrollIntoView({block: 'center'});", share_btn
        )
        human_delay(0.5, 1)
        try:
            share_btn.click()
        except Exception:
            driver.execute_script("arguments[0].click();", share_btn)
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

        # Click "Share now"
        human_delay(0.5, 1)
        try:
            share_now_btn.click()
        except Exception:
            # Fallback: JavaScript click
            driver.execute_script("arguments[0].click();", share_now_btn)
        logger.info("  Clicked 'Share now'")
        human_delay(3, 5)

        # Step 4: Wait for share to complete
        logger.info("  Waiting for share to complete...")

        # Watch for the share menu/dialog to close
        max_wait = 30
        start_time = time.time()
        while time.time() - start_time < max_wait:
            try:
                # Check if share confirmation toast appeared
                toast_selectors = [
                    "//*[contains(text(), 'Udost\u0119pniono')]",
                    "//*[contains(text(), 'Shared')]",
                    "//*[contains(text(), 'udost\u0119pniono')]",
                ]
                for t_sel in toast_selectors:
                    toasts = driver.find_elements(By.XPATH, t_sel)
                    if any(t.is_displayed() for t in toasts):
                        logger.info("  Share confirmation toast detected!")
                        human_delay(1, 2)
                        break
                else:
                    time.sleep(1)
                    continue
                break
            except Exception:
                time.sleep(1)
        else:
            logger.warning(f"  No confirmation toast after {max_wait}s, assuming share completed")

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

        logger.info(f"  Successfully shared post from {source_name}")
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

    # Early exit only if BOTH no new page posts AND no groups configured.
    # When MONITORED_GROUPS is non-empty we still need to spin up Selenium
    # to scrape them (authenticated) before deciding.
    if not new_page_posts and not MONITORED_GROUPS:
        logger.info("No new posts to share and no groups configured.")
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

        # Step 6: Authenticated group scrape (uses same driver)
        new_posts = list(new_page_posts)
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
