#!/usr/bin/env python3
"""
Facebook Messenger Notification Script
Sends notification about completed article scraping to Facebook Page Messenger.

Usage:
    from send_facebook_notification import send_article_notification
    send_article_notification(article_count=5)
    
Or standalone:
    python send_facebook_notification.py 5
"""

import requests
import sys
from datetime import datetime

# Facebook API Configuration
# Page ID: 608399975689819 (Piotr Kirklewski page)
FACEBOOK_PAGE_ACCESS_TOKEN = "EAAQ9FTaKkbcBO89bsj1BCTYRdb7xzbMeqBZCwxPASFIXJENRQc2CoTyTyoHURoW88yViqWH9m7nu22UEf3m2P1ugAnZA6CSiAPwIWh5u0qwcIPRhB0QEQIPZBiY6F4rrwAy3Llx1UO1ZAdRSY70SkVNpeGVYx1ZCr2vnJeBAEWhSRZCQ63o4oykc0Awe80GGO5TWZArfC070nmnAJZBlMwZDZD"
GRAPH_API_VERSION = "v18.0"
GRAPH_API_URL = f"https://graph.facebook.com/{GRAPH_API_VERSION}/me/messages"

# ============================================
# RECIPIENT LIST - Add new user IDs here
# ============================================
RECIPIENTS = [
    {"id": "9046957285410912", "name": "Piotr Kirklewski"},
    {"id": "9710314939004176", "name": "Małgorzata Kirklewska"},
    #{"id": "9855168997944738", "name": "Jagoda Pernal"},
    
    # Add more recipients below:
    # {"id": "USER_ID_HERE", "name": "Name Here"},
]


def send_facebook_message(recipient_id: str, message: str) -> bool:
    """
    Send a message to a single recipient via Facebook Messenger API.
    
    Args:
        recipient_id: The Facebook user ID to send to
        message: The text message to send
        
    Returns:
        True if successful, False otherwise
    """
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {FACEBOOK_PAGE_ACCESS_TOKEN}"
    }
    
    payload = {
        "recipient": {
            "id": recipient_id
        },
        "message": {
            "text": message
        }
    }
    
    try:
        response = requests.post(GRAPH_API_URL, headers=headers, json=payload, timeout=30)
        
        if response.status_code == 200:
            return True
        else:
            print(f"❌ Facebook API error for {recipient_id}: {response.status_code}")
            print(f"Response: {response.text}")
            return False
            
    except requests.exceptions.RequestException as e:
        print(f"❌ Request failed for {recipient_id}: {str(e)}")
        return False


def send_to_all_recipients(message: str) -> tuple:
    """
    Send a message to all recipients in the RECIPIENTS list.
    
    Args:
        message: The text message to send
        
    Returns:
        Tuple of (successful_count, failed_count)
    """
    successful = 0
    failed = 0
    
    for recipient in RECIPIENTS:
        recipient_id = recipient["id"]
        recipient_name = recipient["name"]
        
        if send_facebook_message(recipient_id, message):
            print(f"✅ Sent to {recipient_name}")
            successful += 1
        else:
            print(f"❌ Failed to send to {recipient_name}")
            failed += 1
    
    return successful, failed


def send_article_notification(article_count: int, successful: int = None, failed: int = None) -> bool:
    """
    Send a notification about completed article scraping to all recipients.
    
    Args:
        article_count: Total number of articles processed
        successful: Number of successfully created articles (optional)
        failed: Number of failed articles (optional)
        
    Returns:
        True if all notifications sent successfully, False otherwise
    """
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    # Build message
    message_lines = [
        "📰 Christian News Scraper Report",
        f"⏰ {timestamp}",
        "",
    ]
    
    if successful is not None and failed is not None:
        message_lines.extend([
            f"✅ Successful: {successful}",
            f"❌ Failed: {failed}",
            f"📊 Total processed: {article_count}",
        ])
    else:
        message_lines.append(f"📊 Articles created: {article_count}")
    
    message_lines.extend([
        "",
        "Script completed on Linux Ubuntu."
    ])
    
    message = "\n".join(message_lines)
    
    print(f"\n📤 Sending notifications to {len(RECIPIENTS)} recipients...")
    sent_ok, sent_fail = send_to_all_recipients(message)
    print(f"📊 Results: {sent_ok} sent, {sent_fail} failed")
    
    return sent_fail == 0


def main():
    """
    Main function for standalone execution.
    Usage: python send_facebook_notification.py <article_count> [successful] [failed]
    """
    if len(sys.argv) < 2:
        print("Usage: python send_facebook_notification.py <article_count> [successful] [failed]")
        print("Example: python send_facebook_notification.py 10")
        print("Example: python send_facebook_notification.py 10 8 2")
        print(f"\nConfigured recipients: {len(RECIPIENTS)}")
        for r in RECIPIENTS:
            print(f"  - {r['name']} ({r['id']})")
        sys.exit(1)
    
    try:
        article_count = int(sys.argv[1])
        successful = int(sys.argv[2]) if len(sys.argv) > 2 else None
        failed = int(sys.argv[3]) if len(sys.argv) > 3 else None
        
        success = send_article_notification(article_count, successful, failed)
        sys.exit(0 if success else 1)
        
    except ValueError as e:
        print(f"Error: Invalid argument - {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
