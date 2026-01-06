#!/usr/bin/env python3
"""
Facebook Page Scraper for Boguszów-Gorce News
Scrapes posts from local Facebook pages and news sites, sends notifications via Messenger.

Usage:
    python bg_scraper.py
"""

import asyncio
import hashlib
import json
import os
import re
import requests
from datetime import datetime, timedelta
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup

# ============================================
# FACEBOOK PAGES TO MONITOR
# ============================================
PAGES = [
    "https://www.facebook.com/gminamiastoboguszowgorce",
    "https://www.facebook.com/GornikBoguszowGorce",
    "https://www.facebook.com/MBPCK",
    "https://www.facebook.com/ospboguszow",
]

# Facebook pages with content filtering
FILTERED_PAGES = {
    "https://www.facebook.com/dziennikwalbrzych": {
        "filter": "bogusz",
        "name": "Dziennik Wałbrzyski"
    },
}

# ============================================
# NEWS WEBSITES TO MONITOR (non-Facebook)
# ============================================
NEWS_SITES = {
    "https://walbrzych.dlawas.info/wiadomosci": {
        "filter": "bogusz",
        "name": "Wałbrzych Dla Was"
    },
}

# ============================================
# MESSENGER NOTIFICATION CONFIG
# ============================================
FACEBOOK_PAGE_ACCESS_TOKEN = "EAAQ9FTaKkbcBO89bsj1BCTYRdb7xzbMeqBZCwxPASFIXJENRQc2CoTyTyoHURoW88yViqWH9m7nu22UEf3m2P1ugAnZA6CSiAPwIWh5u0qwcIPRhB0QEQIPZBiY6F4rrwAy3Llx1UO1ZAdRSY70SkVNpeGVYx1ZCr2vnJeBAEWhSRZCQ63o4oykc0Awe80GGO5TWZArfC070nmnAJZBlMwZDZD"
GRAPH_API_VERSION = "v18.0"
GRAPH_API_URL = f"https://graph.facebook.com/{GRAPH_API_VERSION}/me/messages"

# ============================================
# RECIPIENT LIST
# ============================================
RECIPIENTS = [
    {"id": "9046957285410912", "name": "Piotr Kirklewski"},
    {"id": "9710314939004176", "name": "Małgorzata Kirklewska"},
    #{"id": "2454096597977681", "name": "Rada Starszych Mesanger Group"}, #messenger chat group - not working
    #{"id": "3287503241398820", "name": "Boguszów-Gorce Newsy i Informacje Group"}, # FB Page group  - not working
]

# ============================================
# DEDUPLICATION CONFIG
# ============================================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SENT_POSTS_FILE = os.path.join(SCRIPT_DIR, "sent_posts.json")
DAYS_TO_KEEP = 7


def load_sent_posts() -> dict:
    """Load previously sent posts from file."""
    if os.path.exists(SENT_POSTS_FILE):
        try:
            with open(SENT_POSTS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {}
    return {}


def save_sent_posts(sent_posts: dict) -> None:
    """Save sent posts to file."""
    with open(SENT_POSTS_FILE, 'w', encoding='utf-8') as f:
        json.dump(sent_posts, f, indent=2, ensure_ascii=False)


def cleanup_old_posts(sent_posts: dict) -> dict:
    """Remove posts older than DAYS_TO_KEEP."""
    cutoff = datetime.now() - timedelta(days=DAYS_TO_KEEP)
    cutoff_str = cutoff.isoformat()
    
    cleaned = {
        post_id: timestamp 
        for post_id, timestamp in sent_posts.items() 
        if timestamp > cutoff_str
    }
    return cleaned


def get_post_id(post: dict) -> str:
    """Generate unique ID for a post (uses link or stable text hash)."""
    if post.get('link'):
        return post['link']
    # Fallback: stable MD5 hash of first 200 chars of text
    text_to_hash = post.get('text', '')[:200].encode('utf-8')
    return hashlib.md5(text_to_hash).hexdigest()


def is_already_sent(post: dict, sent_posts: dict) -> bool:
    """Check if post was already sent."""
    post_id = get_post_id(post)
    return post_id in sent_posts


def mark_as_sent(post: dict, sent_posts: dict) -> None:
    """Mark post as sent."""
    post_id = get_post_id(post)
    sent_posts[post_id] = datetime.now().isoformat()


async def scrape_fb_page(url: str) -> list[dict]:
    """Scrape posts from a Facebook page."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        )
        page = await context.new_page()
        
        await page.goto(url, wait_until="networkidle")
        await page.wait_for_timeout(3000)
        
        await page.evaluate("window.scrollBy(0, 1000)")
        await page.wait_for_timeout(2000)
        
        html = await page.content()
        await browser.close()
        
    return parse_fb_posts(html, url)


def parse_fb_posts(html: str, source_url: str) -> list[dict]:
    """Parse posts from Facebook page HTML."""
    soup = BeautifulSoup(html, 'html.parser')
    posts = []
    
    post_divs = soup.find_all('div', attrs={'aria-posinset': True})
    
    for post_div in post_divs:
        post = {}
        
        # Extract text content
        text_divs = post_div.find_all('div', attrs={'dir': 'auto'})
        post['text'] = ' '.join(d.get_text(strip=True) for d in text_divs[:3])
        
        # Extract timestamp
        time_pattern = re.search(r'(\d+\s*(?:godz|min|sek)|Wczoraj|Teraz)', 
                                  post_div.get_text())
        post['time'] = time_pattern.group(1) if time_pattern else None
        
        # Extract images
        imgs = post_div.find_all('img', src=re.compile(r'scontent.*\.jpg'))
        post['images'] = [img['src'] for img in imgs[:5]]
        
        # Extract post link - includes videos, reels, watch
        links = post_div.find_all('a', href=re.compile(r'/posts/|/photo/|/videos/|/watch/|/reel/'))
        if links:
            href = links[0].get('href', '')
            clean_href = href.split('?')[0]
            if clean_href.startswith('/'):
                post['link'] = f"https://www.facebook.com{clean_href}"
            else:
                post['link'] = clean_href
        
        post['source'] = source_url
        post['scraped_at'] = datetime.now().isoformat()
        
        if post['text'] and len(post['text']) > 20:
            posts.append(post)
    
    return posts


async def scrape_news_site(url: str) -> list[dict]:
    """Scrape articles from a news website (non-Facebook)."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        )
        page = await context.new_page()
        
        await page.goto(url, wait_until="networkidle")
        await page.wait_for_timeout(2000)
        
        html = await page.content()
        await browser.close()
        
    return parse_dlawas_articles(html, url)


def parse_dlawas_articles(html: str, source_url: str) -> list[dict]:
    """Parse articles from walbrzych.dlawas.info."""
    soup = BeautifulSoup(html, 'html.parser')
    posts = []
    
    # Find all article containers
    articles = soup.find_all('article', class_='category-item')
    
    for article in articles:
        post = {}
        
        # Extract title and link
        title_elem = article.find('h2', class_='categoryItemTitle')
        if title_elem:
            link_elem = title_elem.find('a')
            if link_elem:
                post['text'] = link_elem.get_text(strip=True)
                href = link_elem.get('href', '')
                if href.startswith('/'):
                    post['link'] = f"https://walbrzych.dlawas.info{href}"
                else:
                    post['link'] = href
        
        # Extract timestamp - look for "X godziny temu", "X minut temu", etc.
        time_span = article.find('span', string=re.compile(r'(godzin|minut|sekund|Teraz)', re.IGNORECASE))
        if not time_span:
            # Try finding by fa-clock-o icon
            clock_icon = article.find('i', class_='fa-clock-o')
            if clock_icon and clock_icon.parent:
                time_text = clock_icon.parent.get_text(strip=True)
                time_match = re.search(r'(\d+\s*(?:godzin|minut|sekund).*?temu|Teraz)', time_text, re.IGNORECASE)
                if time_match:
                    post['time'] = time_match.group(1)
        else:
            post['time'] = time_span.get_text(strip=True)
        
        # Extract description/preview
        paragraphs = article.find_all('p')
        for p in paragraphs:
            text = p.get_text(strip=True)
            # Skip timestamp paragraphs
            if text and not re.match(r'^\d+\s*(godzin|minut)', text, re.IGNORECASE):
                if len(text) > 30:  # Actual content
                    post['text'] = f"{post.get('text', '')} - {text}"
                    break
        
        # Extract image
        img = article.find('img', class_='categoryItemThumb')
        if img and img.get('src'):
            post['images'] = [img['src']]
        
        post['source'] = source_url
        post['scraped_at'] = datetime.now().isoformat()
        
        if post.get('text') and len(post['text']) > 10:
            posts.append(post)
    
    return posts


def is_today(time_str: str) -> bool:
    """Check if timestamp indicates today."""
    if not time_str:
        return False
    time_lower = time_str.lower()
    # Polish time indicators for "today"
    return any(x in time_lower for x in ['godz', 'min', 'sek', 'teraz', 'godzin', 'minut', 'sekund'])


def send_facebook_message(recipient_id: str, message: str) -> bool:
    """Send a message via Facebook Messenger API."""
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {FACEBOOK_PAGE_ACCESS_TOKEN}"
    }
    
    payload = {
        "recipient": {"id": recipient_id},
        "message": {"text": message}
    }
    
    try:
        response = requests.post(GRAPH_API_URL, headers=headers, json=payload, timeout=30)
        return response.status_code == 200
    except requests.exceptions.RequestException:
        return False


def send_to_all_recipients(message: str) -> tuple:
    """Send a message to all recipients."""
    successful = 0
    for recipient in RECIPIENTS:
        if send_facebook_message(recipient["id"], message):
            print(f"  ✅ Sent to {recipient['name']}")
            successful += 1
        else:
            print(f"  ❌ Failed to send to {recipient['name']}")
    return successful


def notify_post(post: dict) -> None:
    """Send notification about a single post."""
    source_url = post['source']
    
    # Determine source name
    if 'facebook.com' in source_url:
        source_name = source_url.split('/')[-1]
    elif 'dlawas.info' in source_url:
        source_name = "Wałbrzych DlaWas"
    else:
        source_name = source_url.split('/')[2]  # domain
    
    message_lines = [
        f"📰 Boguszów News",
        f"📍 {source_name}",
        f"⏰ {post.get('time', 'Nieznana godzina')}",
        "",
        post['text'][:400],
    ]
    
    if post.get('link'):
        message_lines.extend(["", f"🔗 {post['link']}"])
    
    message = "\n".join(message_lines)
    print(f"Sending notification for post: {post['text'][:50]}...")
    send_to_all_recipients(message)


async def main():
    """Main function - scrape pages and send notifications."""
    all_posts = []
    
    # Load and cleanup sent posts
    sent_posts = load_sent_posts()
    sent_posts = cleanup_old_posts(sent_posts)
    
    # Scrape regular Facebook pages
    for url in PAGES:
        print(f"Scraping: {url}")
        try:
            posts = await scrape_fb_page(url)
            today_posts = [p for p in posts if is_today(p.get('time'))]
            all_posts.extend(today_posts)
            print(f"  Found {len(today_posts)} posts from today")
        except Exception as e:
            print(f"  ❌ Error scraping {url}: {e}")
    
    # Scrape filtered Facebook pages
    for url, config in FILTERED_PAGES.items():
        filter_text = config["filter"].lower()
        print(f"Scraping: {url} (filter: {filter_text})")
        try:
            posts = await scrape_fb_page(url)
            today_posts = [p for p in posts if is_today(p.get('time'))]
            filtered_posts = [
                p for p in today_posts 
                if filter_text in p.get('text', '').lower()
            ]
            all_posts.extend(filtered_posts)
            print(f"  Found {len(today_posts)} posts from today, {len(filtered_posts)} matching filter")
        except Exception as e:
            print(f"  ❌ Error scraping {url}: {e}")
    
    # Scrape news websites
    for url, config in NEWS_SITES.items():
        filter_text = config["filter"].lower()
        site_name = config["name"]
        print(f"Scraping: {site_name} (filter: {filter_text})")
        try:
            posts = await scrape_news_site(url)
            today_posts = [p for p in posts if is_today(p.get('time'))]
            filtered_posts = [
                p for p in today_posts 
                if filter_text in p.get('text', '').lower()
            ]
            all_posts.extend(filtered_posts)
            print(f"  Found {len(today_posts)} articles from today, {len(filtered_posts)} matching filter")
        except Exception as e:
            print(f"  ❌ Error scraping {url}: {e}")
    
    # Filter out already sent
    new_posts = [p for p in all_posts if not is_already_sent(p, sent_posts)]
    skipped = len(all_posts) - len(new_posts)
    if skipped > 0:
        print(f"\nSkipped {skipped} already sent post(s)")
    
    # Save posts.json
    with open(os.path.join(SCRIPT_DIR, 'posts.json'), 'w', encoding='utf-8') as f:
        json.dump(all_posts, f, ensure_ascii=False, indent=2)
    print(f"Saved {len(all_posts)} posts to posts.json")
    
    # Send notifications
    if new_posts:
        print(f"\nSending {len(new_posts)} NEW notification(s) to Messenger...")
        for post in new_posts:
            notify_post(post)
            mark_as_sent(post, sent_posts)
        save_sent_posts(sent_posts)
        print(f"\n✅ Done! Sent {len(new_posts)} notification(s)")
    else:
        save_sent_posts(sent_posts)
        print("\nNo new posts - no notifications sent.")
    
    return new_posts


if __name__ == "__main__":
    posts = asyncio.run(main())
