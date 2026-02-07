#!/usr/bin/env python3
"""
Facebook Page Scraper for Boguszow-Gorce News - SELENIUM VERSION
Scrapes posts from Dziennik Walbrzych, Policja Walbrzych, and TV Walbrzych.
Filters articles containing "Bogusz" (case-insensitive).
Posts to Facebook using link preview (Open Graph thumbnails).

Version: 1.0.0 (2026-02-07)
Changes:
  - Initial version based on Walbrzych scraper v3.0.0
  - Added Bogusz keyword filter for article relevance
  - Docker Selenium only (USE_DOCKER always True)
  - Targets Boguszow-Gorce Newsy i Informacje FB page
"""

import json
import re
import requests
import logging
import sys
import time
import random
import os
import fcntl
import atexit
from datetime import datetime, timedelta
from pathlib import Path
from bs4 import BeautifulSoup

from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.action_chains import ActionChains

# ============================================
# CONFIGURATION
# ============================================

TEST_MODE = False
USE_DOCKER = True  # Always Docker

ENABLE_DZIENNIK = True
ENABLE_POLICJA = True
ENABLE_TVWALBRZYCH = True

# Bogusz filter keyword (case-insensitive)
BOGUSZ_FILTER = "bogusz"

# Paths
PROJECT_ROOT = Path(__file__).parent.parent
LOG_FILE = PROJECT_ROOT / "logs" / "bg_scraper_selenium.log"
SENT_POSTS_FILE = PROJECT_ROOT / "data" / "sent_posts.json"

# Facebook page configuration
FB_PAGE_URL = "https://www.facebook.com/profile.php?id=100027689516729"
FB_PROFILE_LINK = "fb.com/profile.php?id=100027689516729"
FB_PAGE_NAME = "Boguszow-Gorce Newsy i Informacje"

# Delays
LINK_PREVIEW_DELAY = 15  # seconds for Facebook to load thumbnail
MIN_DELAY_BETWEEN_POSTS = 10  # seconds
MAX_DELAY_BETWEEN_POSTS = 20  # seconds

# Process isolation
LOCK_FILE = PROJECT_ROOT / "locks" / "scraper.lock"

# Ensure log directory exists
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

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

    # Ensure locks directory exists
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
# HELPER FUNCTIONS
# ============================================

def human_delay(min_sec=1.0, max_sec=3.0):
    time.sleep(random.uniform(min_sec, max_sec))


def load_sent_posts():
    if SENT_POSTS_FILE.exists():
        try:
            with open(SENT_POSTS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_sent_posts(sent_posts):
    if not TEST_MODE:
        SENT_POSTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(SENT_POSTS_FILE, 'w', encoding='utf-8') as f:
            json.dump(sent_posts, f, indent=2, ensure_ascii=False)


def cleanup_old_posts(sent_posts, days=7):
    cutoff = datetime.now() - timedelta(days=days)
    cutoff_str = cutoff.isoformat()
    return {k: v for k, v in sent_posts.items() if v > cutoff_str}


def get_post_id(url, source_prefix):
    """Generate unique ID for deduplication."""
    if not url:
        return ""

    if source_prefix == "dziennik":
        # Extract slug from URL like: dziennik.walbrzych.pl/article-slug/
        # Use [^/?]+ to stop at / or ? (ignores query params like fbclid)
        match = re.search(r'dziennik\.walbrzych\.pl/([^/?]+)', url)
        if match:
            return f"dziennik_{match.group(1)}"

    if source_prefix == "policja":
        # Extract ID from URL like: /dba/aktualnosci/bieza/163631,Title.html
        match = re.search(r'/(\d+),', url)
        if match:
            return f"policja_{match.group(1)}"

    if source_prefix == "tvwalbrzych":
        # Extract article ID from URL
        match = re.search(r'/pl/\d+_[^/]+/(\d+)_', url)
        if match:
            return f"tvwalbrzych_{match.group(1)}"

    return url


def clean_text(text):
    """Clean article text for posting."""
    if not text:
        return ""
    # Remove URLs
    text = re.sub(r'https?://[^\s]+', '', text)
    # Remove extra whitespace
    text = re.sub(r'\s+', ' ', text).strip()
    return text[:500]  # Limit length


def contains_bogusz(text):
    """Check if text contains 'bogusz' (case-insensitive)."""
    if not text:
        return False
    return BOGUSZ_FILTER in text.lower()


# ============================================
# SCRAPING FUNCTIONS
# ============================================

def scrape_dziennik_walbrzych():
    """Scrape articles from dziennik.walbrzych.pl"""
    url = "https://dziennik.walbrzych.pl/"
    logger.info("Scraping Dziennik Walbrzych...")

    try:
        response = requests.get(url, timeout=30, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
        response.encoding = 'utf-8'
        soup = BeautifulSoup(response.text, 'html.parser')
        posts = []
        seen_urls = set()

        for article in soup.select('div.kontener'):
            post = {}
            title_link = article.select_one('div.tytul a')
            if not title_link:
                continue

            href = title_link.get('href', '')
            if href.startswith('/'):
                href = f"https://dziennik.walbrzych.pl{href}"

            # Skip non-article links
            if '@' in href or 'polityka' in href:
                continue

            # Deduplicate
            if href in seen_urls:
                continue
            seen_urls.add(href)

            post['link'] = href
            post['text'] = title_link.get('title') or title_link.get_text(strip=True)
            post['source'] = 'dziennik'
            post['id'] = get_post_id(href, 'dziennik')

            if post.get('text') and len(post['text']) > 10:
                posts.append(post)

        logger.info(f"Dziennik: found {len(posts)} articles")
        return posts[:20]

    except Exception as e:
        logger.error(f"Error scraping Dziennik: {e}")
        return []


def scrape_policja_walbrzych():
    """Scrape articles from walbrzych.policja.gov.pl"""
    url = "https://walbrzych.policja.gov.pl/dba/aktualnosci/bieza"
    logger.info("Scraping Policja Walbrzych...")

    try:
        response = requests.get(url, timeout=30, verify=False, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
        response.encoding = 'utf-8'
        soup = BeautifulSoup(response.text, 'html.parser')
        posts = []
        seen_urls = set()

        # Accept articles from last 3 days
        recent_dates = []
        for i in range(3):  # Today, yesterday, day before
            d = datetime.now() - timedelta(days=i)
            recent_dates.append(d.strftime('%d.%m.%Y'))

        logger.info(f"Accepting dates: {recent_dates}")

        for article in soup.select('li.news'):
            post = {}
            link_elem = article.select_one('a')
            if not link_elem:
                continue

            href = link_elem.get('href', '')

            # IMPORTANT: Only accept LOCAL Walbrzych articles (path starts with /dba/)
            # Skip national policja.pl articles (path starts with /pol/)
            if href.startswith('/dba/'):
                href = f"https://walbrzych.policja.gov.pl{href}"
            elif href.startswith('https://walbrzych.policja.gov.pl'):
                pass  # Already full URL
            else:
                # Skip national or other links
                logger.debug(f"Skipping non-local link: {href}")
                continue

            # Deduplicate
            if href in seen_urls:
                continue
            seen_urls.add(href)

            post['link'] = href

            # Get title
            title_elem = article.select_one('a strong')
            if title_elem:
                post['text'] = title_elem.get_text(strip=True)

            # Get description
            desc_elem = article.select_one('a p')
            if desc_elem:
                desc = desc_elem.get_text(strip=True)
                if desc and post.get('text'):
                    post['text'] = f"{post['text']}\n\n{desc}"

            post['source'] = 'policja'
            post['id'] = get_post_id(href, 'policja')

            # Filter articles from recent days
            date_elem = article.select_one('span.data')
            if date_elem:
                date_text = date_elem.get_text(strip=True)
                is_recent = any(d in date_text for d in recent_dates)
                if is_recent and post.get('text') and len(post['text']) > 10:
                    posts.append(post)

        logger.info(f"Policja: found {len(posts)} LOCAL articles from last 3 days")
        return posts[:20]

    except Exception as e:
        logger.error(f"Error scraping Policja: {e}")
        return []


def scrape_tvwalbrzych():
    """Scrape articles from tvwalbrzych.pl"""
    url = "https://tvwalbrzych.pl/"
    logger.info("Scraping TV Walbrzych...")

    try:
        response = requests.get(url, timeout=30, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
        response.encoding = 'utf-8'
        soup = BeautifulSoup(response.text, 'html.parser')
        posts = []
        seen_urls = set()

        # Find all article links - pattern: /pl/CATEGORY/ID_SLUG.html
        # Example: /pl/473_na-sygnale/22519_pol-roku-kontroli-na-granicy-z-niemcami-fakty-zamiast-mitow.html

        for link in soup.find_all('a', href=True):
            href = link.get('href', '')

            # Match article URLs with numeric ID
            match = re.search(r'/pl/\d+_[^/]+/(\d+)_([^/]+)\.html', href)
            if not match:
                continue

            article_id = match.group(1)

            # Build full URL
            if href.startswith('/'):
                href = f"https://tvwalbrzych.pl{href}"
            elif not href.startswith('http'):
                continue

            # Skip non-article pages
            if '/15_fotorelacje/' in href:  # Skip photo galleries
                continue
            if '/18_co_gdzie_kiedy/' in href:  # Skip events
                continue
            if '/669_nekrologi/' in href:  # Skip obituaries
                continue
            if '/655_przytul-mnie/' in href:  # Skip pet adoption
                continue
            if '/629_urzad-pracy/' in href:  # Skip job listings
                continue

            # Deduplicate
            if href in seen_urls:
                continue
            seen_urls.add(href)

            # Get title - try to find specific title element first
            title_elem = link.select_one('.ticker__link__title')
            if title_elem:
                title = title_elem.get_text(strip=True)
            else:
                # Fallback: get full text and strip date patterns
                title = link.get_text(strip=True)

                # Remove date patterns like "7 stycznia", "11:14", etc.
                title = re.sub(r'^[*]+\s*\d{1,2}:\d{2}\s*', '', title)  # time
                title = re.sub(
                    r'^[*]+\s*\d{1,2}\s+(?:stycznia|lutego|marca|kwietnia|maja|czerwca|'
                    r'lipca|sierpnia|wrze[s]nia|pa[z]dziernika|listopada|grudnia)\s*',
                    '', title, flags=re.IGNORECASE
                )
                title = title.strip()

            # Skip if no title or too short
            if not title or len(title) < 10:
                continue

            # Skip navigation/menu items
            if title in ['TOP', 'Sport', 'Kultura', 'Region', 'Wiadomosci']:
                continue

            post = {
                'link': href,
                'text': title,
                'source': 'tvwalbrzych',
                'id': f"tvwalbrzych_{article_id}"
            }
            posts.append(post)

        # Remove duplicates by ID (keep first occurrence)
        unique_posts = []
        seen_ids = set()
        for post in posts:
            if post['id'] not in seen_ids:
                seen_ids.add(post['id'])
                unique_posts.append(post)

        logger.info(f"TV Walbrzych: found {len(unique_posts)} articles")
        return unique_posts[:20]

    except Exception as e:
        logger.error(f"Error scraping TV Walbrzych: {e}")
        return []


# ============================================
# SELENIUM FUNCTIONS
# ============================================

def setup_driver():
    """Setup Chrome driver using Docker Selenium."""
    from docker_selenium import get_docker_driver
    logger.info("Using Docker Selenium...")
    return get_docker_driver(max_retries=3)


def setup_driver_with_retry():
    """Setup Chrome driver with automatic recovery and retry on failure."""
    try:
        return setup_driver()
    except Exception as e:
        logger.error(f"Docker Selenium failed: {e}")
        logger.error("=" * 60)
        logger.error("DOCKER TROUBLESHOOTING:")
        logger.error("  1. Check container: docker ps")
        logger.error("  2. View logs: docker logs bg-selenium-chrome")
        logger.error("  3. Restart: docker compose -f docker-compose.yml restart")
        logger.error("  4. Re-login: python src/docker_fb_login.py")
        logger.error("=" * 60)
        raise


def prepare_caption(post):
    """Prepare caption in the established format for Boguszow-Gorce.

    Format:
        [heart] Wesprzyj lokalna fundacje. Przekaz 1.5% podatku. KRS: 0000498479
        [arrow] Wiecej: fb.com/profile.php?id=100027689516729
        [Article text, cleaned, max 500 chars]

        #BoguszowGorce #Boguszow #DolnySlask
    """
    text = clean_text(post['text'])

    lines = [
        "\u2764\ufe0f Wesprzyj lokaln\u0105 fundacj\u0119. Przeka\u017c 1.5% podatku. KRS: 0000498479",
        f"\ud83d\udc49 Wi\u0119cej: {FB_PROFILE_LINK}",
        text,
        "",
        "#Bogusz\u00f3wGorce #Bogusz\u00f3w #DolnyŚl\u0105sk"
    ]

    return "\n".join(lines)


def post_to_facebook(driver, post):
    """Post article to Facebook using link preview method."""
    source = post.get('source', 'dziennik')
    caption = prepare_caption(post)

    source_names = {
        'dziennik': 'Dziennik',
        'policja': 'Policja',
        'tvwalbrzych': 'TV Walbrzych'
    }
    source_name = source_names.get(source, 'Article')

    return post_via_link_preview(driver, post, caption, source_name)


def post_via_link_preview(driver, post, caption, source_name="Article"):
    """Post article using standard link preview in composer.

    Works for: Dziennik, TV Walbrzych, Policja - any source with Open Graph metadata.
    """

    link = post['link']

    logger.info(f"{source_name.upper()}: Using link preview strategy...")
    logger.info(f"Navigating to FB page...")
    driver.get(FB_PAGE_URL)
    human_delay(4, 6)

    # ============================================
    # LOGIN CHECK
    # ============================================
    if len(driver.find_elements(By.NAME, "email")) > 0:
        logger.warning("LOGIN DETECTED! Pausing 120s for manual login...")
        time.sleep(120)
        driver.get(FB_PAGE_URL)
        human_delay(4, 6)

    # ============================================
    # SWITCH TO PAGE PROFILE (if needed)
    # ============================================
    try:
        switch_btn = WebDriverWait(driver, 5).until(
            EC.element_to_be_clickable((By.XPATH, "//span[text()='Prze\u0142\u0105cz teraz']"))
        )
        switch_btn.click()
        logger.info("Clicked 'Przelacz teraz'")
        human_delay(2, 3)

        confirm_btn = WebDriverWait(driver, 5).until(
            EC.element_to_be_clickable((By.XPATH, "//div[@role='dialog']//span[text()='Prze\u0142\u0105cz']"))
        )
        confirm_btn.click()
        logger.info("Confirmed switch")
        human_delay(3, 5)
    except Exception:
        pass

    # ============================================
    # CLICK "CO SLYCHAC?" TO OPEN CREATOR
    # ============================================
    logger.info("Looking for post creation area...")

    try:
        post_box = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable((By.XPATH, "//span[contains(text(), 'Co s\u0142ycha\u0107')]"))
        )
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", post_box)
        human_delay(0.5, 1)
        post_box.click()
        logger.info("Clicked post creation area")
        human_delay(3, 4)
    except Exception as e:
        logger.error(f"Could not find post creation area: {e}")
        return False

    # ============================================
    # WAIT FOR DIALOG
    # ============================================
    try:
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.XPATH, "//div[@role='dialog']"))
        )
        logger.info("Post dialog opened")
    except Exception:
        logger.error("Post dialog did not open")
        return False

    human_delay(1, 2)

    # ============================================
    # STEP 1: PASTE ENTIRE URL FIRST
    # ============================================
    logger.info("Pasting article URL first...")

    try:
        text_area = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.XPATH, "//div[@role='dialog']//div[@role='textbox'][@contenteditable='true']"))
        )
        text_area.click()
        human_delay(0.5, 1)

        # Paste ENTIRE URL via JavaScript (atomic)
        driver.execute_script("""
            const textArea = arguments[0];
            const url = arguments[1];
            textArea.focus();
            document.execCommand('insertText', false, url);
        """, text_area, link)
        logger.info("URL pasted via JavaScript")

    except Exception as e:
        logger.error(f"Error pasting URL: {e}")
        return False

    # ============================================
    # STEP 2: WAIT FOR URL PREVIEW TO LOAD
    # ============================================
    logger.info(f"Waiting {LINK_PREVIEW_DELAY}s for link preview thumbnail...")
    time.sleep(LINK_PREVIEW_DELAY)

    # ============================================
    # STEP 3: MOVE CURSOR TO BEGINNING (Ctrl+Home)
    # ============================================
    logger.info("Moving cursor to beginning with Ctrl+Home...")

    try:
        text_area.click()
        human_delay(0.3, 0.5)

        actions = ActionChains(driver)
        actions.key_down(Keys.CONTROL).send_keys(Keys.HOME).key_up(Keys.CONTROL)
        actions.perform()
        human_delay(0.3, 0.5)
        logger.info("Cursor at beginning")

    except Exception as e:
        logger.error(f"Error moving cursor: {e}")
        return False

    # ============================================
    # STEP 4: INSERT TWO NEWLINES (Shift+Enter twice)
    # ============================================
    logger.info("Inserting two newlines above URL...")

    try:
        # Shift+Enter twice to create space above URL
        for _ in range(2):
            actions = ActionChains(driver)
            actions.key_down(Keys.SHIFT).send_keys(Keys.ENTER).key_up(Keys.SHIFT)
            actions.perform()
            human_delay(0.1, 0.2)

        logger.info("Two newlines inserted")
        human_delay(0.5, 1)

    except Exception as e:
        logger.error(f"Error inserting newlines: {e}")
        return False

    # ============================================
    # STEP 5: MOVE CURSOR UP TWICE (UP arrow key)
    # ============================================
    logger.info("Moving cursor up twice...")

    try:
        for _ in range(2):
            actions = ActionChains(driver)
            actions.send_keys(Keys.ARROW_UP)
            actions.perform()
            human_delay(0.1, 0.2)

        logger.info("Cursor moved up")
        human_delay(0.3, 0.5)

    except Exception as e:
        logger.error(f"Error moving cursor up: {e}")
        return False

    # ============================================
    # STEP 6: PASTE CAPTION (line by line with Shift+Enter)
    # ============================================
    logger.info("Pasting caption...")

    try:
        # Split caption into lines and paste each with Shift+Enter
        # JavaScript insertText doesn't preserve \n as line breaks in FB
        lines = caption.split('\n')

        for i, line in enumerate(lines):
            if line:
                # Paste line via JavaScript (atomic)
                driver.execute_script("""
                    const textArea = arguments[0];
                    const text = arguments[1];
                    document.execCommand('insertText', false, text);
                """, text_area, line)
                human_delay(0.1, 0.2)

            # Add Shift+Enter after each line (except the last)
            if i < len(lines) - 1:
                actions = ActionChains(driver)
                actions.key_down(Keys.SHIFT).send_keys(Keys.ENTER).key_up(Keys.SHIFT)
                actions.perform()
                human_delay(0.05, 0.1)

        logger.info("Caption pasted above URL")
        human_delay(2, 3)

    except Exception as e:
        logger.error(f"Error pasting caption: {e}")
        return False

    # ============================================
    # STEP 7: CLOSE FB.COM PREVIEW IF IT APPEARED
    # ============================================
    # When caption contains fb.com link, Facebook may create a second preview
    # We need to close it so the article preview remains
    logger.info("Checking for fb.com preview to close...")

    try:
        human_delay(1, 2)

        # The fb.com preview has an X button with aria-label="Usun podglad linku z posta"
        # First check if fb.com preview exists (look for "fb.com" text in preview area)
        fb_preview_exists = False
        try:
            fb_elements = driver.find_elements(
                By.XPATH,
                "//div[@role='dialog']//div[text()='fb.com'] | //div[@role='dialog']//span[text()='fb.com']"
            )
            for elem in fb_elements:
                if elem.is_displayed():
                    fb_preview_exists = True
                    logger.info("Found fb.com preview element")
                    break
        except Exception:
            pass

        if fb_preview_exists:
            # Click the X button to close fb.com preview
            close_selectors = [
                "//div[@aria-label='Usu\u0144 podgl\u0105d linku z posta']",
                "//div[@role='button'][@aria-label='Usu\u0144 podgl\u0105d linku z posta']",
                "//span[@aria-label='Usu\u0144 podgl\u0105d linku z posta']",
            ]

            closed = False
            for selector in close_selectors:
                try:
                    close_btns = driver.find_elements(By.XPATH, selector)
                    for btn in close_btns:
                        if btn.is_displayed():
                            btn.click()
                            logger.info(f"Closed fb.com preview via: {selector}")
                            closed = True
                            break
                    if closed:
                        break
                except Exception:
                    continue

            if not closed:
                logger.warning("Could not close fb.com preview - trying JavaScript click")
                try:
                    btn = driver.find_element(
                        By.XPATH,
                        "//div[@aria-label='Usu\u0144 podgl\u0105d linku z posta']"
                    )
                    driver.execute_script("arguments[0].click();", btn)
                    logger.info("Closed fb.com preview via JavaScript")
                    closed = True
                except Exception:
                    pass

            if not closed:
                logger.warning("Failed to close fb.com preview!")
        else:
            logger.info("No fb.com preview detected (article preview showing correctly)")

        human_delay(1, 2)

    except Exception as e:
        logger.warning(f"Error checking for fb.com preview: {e}")

    # ============================================
    # CLICK PUBLISH
    # ============================================
    logger.info("Looking for publish button...")
    human_delay(1, 2)

    publish_selectors = [
        "//div[@role='dialog']//span[text()='Dalej']",
        "//div[@role='dialog']//span[text()='Opublikuj']",
        "//div[@role='dialog']//span[text()='Post']",
    ]

    for selector in publish_selectors:
        try:
            btn = WebDriverWait(driver, 5).until(
                EC.element_to_be_clickable((By.XPATH, selector))
            )
            human_delay(0.5, 1)
            btn.click()
            logger.info(f"Clicked: {selector}")
            break
        except Exception:
            continue

    human_delay(3, 4)

    try:
        final_btn = WebDriverWait(driver, 5).until(
            EC.element_to_be_clickable((By.XPATH, "//div[@role='dialog']//span[text()='Opublikuj']"))
        )
        human_delay(0.5, 1)
        final_btn.click()
        logger.info("Clicked final 'Opublikuj'")
    except Exception:
        pass

    human_delay(3, 4)

    try:
        popup_btn = WebDriverWait(driver, 3).until(
            EC.element_to_be_clickable((By.XPATH, "//span[text()='Nie teraz']"))
        )
        popup_btn.click()
        logger.info("Dismissed popup")
    except Exception:
        pass

    human_delay(4, 6)
    logger.info(f"{source_name} post published successfully!")
    return True


# ============================================
# MAIN
# ============================================

def ensure_logged_in_as_page(driver):
    """Navigate to FB page and ensure we're logged in as the page.

    Uses 3-stage approach:
    - STAGE A: Check for immediate "Przelacz profil" modal popup
    - STAGE B: Look for sidebar "Przelacz teraz" button
    - STAGE C: Fallback - use top-right profile menu to switch
    """

    target_profile_name = FB_PAGE_NAME

    logger.info("Opening FB page to verify login...")
    driver.get(FB_PAGE_URL)
    human_delay(4, 6)

    # Check if login needed
    if len(driver.find_elements(By.NAME, "email")) > 0:
        logger.warning("LOGIN DETECTED! Pausing 120s for manual login...")
        time.sleep(120)
        driver.get(FB_PAGE_URL)
        human_delay(4, 6)

    # Handle cookie popup if present
    cookie_selectors = [
        "//button[contains(text(), 'Zezw\u00f3l')]",
        "//button[contains(text(), 'Allow')]",
        "//button[contains(text(), 'Akceptuj')]",
        "//button[contains(text(), 'Accept')]",
        "//span[text()='Zezw\u00f3l na wszystkie pliki cookie']",
    ]

    for sel in cookie_selectors:
        try:
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
                    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", btn)
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

        # Selectors for the profile menu button in top-right corner
        account_menu_selectors = [
            "//div[@role='button'][@aria-label='Tw\u00f3j profil']",
            "//div[@aria-label='Tw\u00f3j profil']",
            "//svg[@aria-label='Tw\u00f3j profil']/ancestor::div[@role='button']",
            "//div[@aria-label='Mechanizmy kontrolne i ustawienia konta']//div[@role='button']",
            "//div[@aria-label='Your profile']",
            "//div[@aria-label='Account controls and settings']//div[@role='button']",
            # Profile picture in header
            "//div[@role='navigation']//div[@role='button']//image",
            "//div[@role='banner']//div[@role='button'][.//image]",
        ]

        # Attempt 1: Standard Selectors with JavaScript Click
        for sel in account_menu_selectors:
            try:
                menu_btn = WebDriverWait(driver, 3).until(
                    EC.presence_of_element_located((By.XPATH, sel))
                )
                driver.execute_script("arguments[0].style.border='3px solid red'", menu_btn)
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
                        "//span[contains(text(), 'See all profiles')]"
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
                    logger.info("STAGE C: Clicked target profile after expanding list")
                    switched = True
                    human_delay(5, 7)
                except Exception as e:
                    logger.error(f"STAGE C failed to find profile in menu: {e}")
                    try:
                        driver.save_screenshot(
                            str(PROJECT_ROOT / "debug" / "debug_stage_c_fail.png")
                        )
                    except Exception:
                        pass
        else:
            logger.error("STAGE C: Could not open menu.")

    if not switched:
        logger.warning("Could not verify profile switch. Attempting to proceed (maybe already correct?)...")
    else:
        logger.info("Profile switch logic completed.")
        human_delay(3, 4)

    return True


def main():
    logger.info("=" * 50)
    logger.info(
        f"START. Dziennik={ENABLE_DZIENNIK}, Policja={ENABLE_POLICJA}, "
        f"TVWalbrzych={ENABLE_TVWALBRZYCH}, TestMode={TEST_MODE}"
    )

    # Acquire lock to prevent concurrent runs
    if not acquire_script_lock():
        logger.error("Another instance is already running. Exiting.")
        sys.exit(1)

    # Register cleanup handlers
    atexit.register(release_script_lock)

    sent_posts = load_sent_posts()
    sent_posts = cleanup_old_posts(sent_posts)

    all_posts = []

    # Scrape sources
    if ENABLE_DZIENNIK:
        all_posts.extend(scrape_dziennik_walbrzych())

    if ENABLE_POLICJA:
        all_posts.extend(scrape_policja_walbrzych())

    if ENABLE_TVWALBRZYCH:
        all_posts.extend(scrape_tvwalbrzych())

    logger.info(f"Total scraped (before Bogusz filter): {len(all_posts)}")

    # ============================================
    # BOGUSZ FILTER - Key difference from Walbrzych version
    # ============================================
    all_posts = [p for p in all_posts if contains_bogusz(p.get('text', ''))]
    logger.info(f"After Bogusz filter: {len(all_posts)} articles contain '{BOGUSZ_FILTER}'")

    # Filter already sent
    new_posts = [p for p in all_posts if p.get('id') and p['id'] not in sent_posts]

    logger.info(f"Total: {len(all_posts)}, New: {len(new_posts)}")

    if not new_posts:
        logger.info("No new posts to publish.")
        save_sent_posts(sent_posts)
        return

    # Separate by source for ordered processing
    dziennik_posts = [p for p in new_posts if p.get('source') == 'dziennik']
    policja_posts = [p for p in new_posts if p.get('source') == 'policja']
    tvwalbrzych_posts = [p for p in new_posts if p.get('source') == 'tvwalbrzych']

    logger.info(
        f"Dziennik: {len(dziennik_posts)}, Policja: {len(policja_posts)}, "
        f"TV Walbrzych: {len(tvwalbrzych_posts)}"
    )

    # Post to Facebook
    driver = None
    try:
        driver = setup_driver_with_retry()

        # STEP 1: Ensure logged in as page
        if not ensure_logged_in_as_page(driver):
            logger.error("Could not verify page login")
            return

        posted_count = 0

        # STEP 2: Process Dziennik posts
        for i, post in enumerate(dziennik_posts):
            logger.info(f"--- Dziennik {i+1}/{len(dziennik_posts)}: {post['text'][:50]}...")

            if TEST_MODE:
                logger.info(f"[TEST MODE] Would post: {post['link']}")
                posted_count += 1
            else:
                if post_to_facebook(driver, post):
                    sent_posts[post['id']] = datetime.now().isoformat()
                    save_sent_posts(sent_posts)
                    posted_count += 1
                    print(f"  Posted: {post['text'][:50]}...")
                else:
                    print(f"  Failed: {post['text'][:50]}...")

            # Delay between posts
            if i < len(dziennik_posts) - 1 or len(policja_posts) > 0 or len(tvwalbrzych_posts) > 0:
                delay = random.randint(MIN_DELAY_BETWEEN_POSTS, MAX_DELAY_BETWEEN_POSTS)
                logger.info(f"Waiting {delay}s before next post...")
                time.sleep(delay)

        # STEP 3: Process Policja posts
        for i, post in enumerate(policja_posts):
            logger.info(f"--- Policja {i+1}/{len(policja_posts)}: {post['text'][:50]}...")

            if TEST_MODE:
                logger.info(f"[TEST MODE] Would post: {post['link']}")
                posted_count += 1
            else:
                if post_to_facebook(driver, post):
                    sent_posts[post['id']] = datetime.now().isoformat()
                    save_sent_posts(sent_posts)
                    posted_count += 1
                    print(f"  Posted: {post['text'][:50]}...")
                else:
                    print(f"  Failed: {post['text'][:50]}...")

            # Delay between posts
            if i < len(policja_posts) - 1 or len(tvwalbrzych_posts) > 0:
                delay = random.randint(MIN_DELAY_BETWEEN_POSTS, MAX_DELAY_BETWEEN_POSTS)
                logger.info(f"Waiting {delay}s before next post...")
                time.sleep(delay)

        # STEP 4: Process TV Walbrzych posts
        for i, post in enumerate(tvwalbrzych_posts):
            logger.info(f"--- TV Walbrzych {i+1}/{len(tvwalbrzych_posts)}: {post['text'][:50]}...")

            if TEST_MODE:
                logger.info(f"[TEST MODE] Would post: {post['link']}")
                posted_count += 1
            else:
                if post_to_facebook(driver, post):
                    sent_posts[post['id']] = datetime.now().isoformat()
                    save_sent_posts(sent_posts)
                    posted_count += 1
                    print(f"  Posted: {post['text'][:50]}...")
                else:
                    print(f"  Failed: {post['text'][:50]}...")

            # Delay between posts
            if i < len(tvwalbrzych_posts) - 1:
                delay = random.randint(MIN_DELAY_BETWEEN_POSTS, MAX_DELAY_BETWEEN_POSTS)
                logger.info(f"Waiting {delay}s before next post...")
                time.sleep(delay)

        logger.info(f"Done! Posted {posted_count}/{len(new_posts)} articles")

    except Exception as e:
        logger.error(f"Critical error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        if driver:
            human_delay(2, 3)
            driver.quit()

    save_sent_posts(sent_posts)


if __name__ == "__main__":
    # Suppress SSL warnings for Policja
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    try:
        main()
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)
