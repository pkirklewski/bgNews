#!/usr/bin/env python3
"""
Docker Facebook Login Helper for Boguszów-Gorce News

Opens a browser window connected to Docker Selenium for Facebook login.
The session is saved in the Docker container for future automated use.

Usage:
    python src/docker_fb_login.py
"""

import sys
import time
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent))

from docker_selenium import (
    get_docker_driver, is_container_running, start_container,
    get_container_status, check_facebook_login
)

FB_PAGE_URL = "https://www.facebook.com/profile.php?id=100027689516729"


def main():
    print("=" * 60)
    print("Docker Facebook Login Helper - Boguszów-Gorce")
    print("=" * 60)
    print()

    # Check container status
    status = get_container_status()
    print(f"Container running: {status['container_running']}")
    print(f"Selenium ready: {status.get('selenium_ready', False)}")
    print()

    if not status['container_running']:
        print("Starting Docker container...")
        if not start_container():
            print("ERROR: Could not start container!")
            print("Run: docker compose up -d")
            sys.exit(1)

    print("Connecting to Docker Selenium...")
    driver = get_docker_driver()

    print()
    print("=" * 60)
    print("INSTRUCTIONS:")
    print("=" * 60)
    print()
    print("1. Open your browser and go to: http://localhost:7901")
    print("   Password: 'secret'")
    print()
    print("2. You will see the Chrome browser running in Docker")
    print()
    print("3. Log into Facebook with your account")
    print()
    print("4. Switch to the 'Boguszów-Gorce Newsy i Informacje' page profile:")
    print("   - Click your profile picture (top right)")
    print("   - Select 'Boguszów-Gorce Newsy i Informacje'")
    print()
    print("5. Once logged in, press ENTER here to verify and save")
    print()
    print("=" * 60)

    # Navigate to Facebook
    driver.get(FB_PAGE_URL)
    print(f"\nNavigated to: {FB_PAGE_URL}")
    print("\nWaiting for you to log in...")
    print("(Open http://localhost:7901 in your browser to see Chrome)")
    print()

    input("Press ENTER when you have logged in and switched to the page profile...")

    # Verify login
    print("\nVerifying login...")
    time.sleep(2)

    if check_facebook_login(driver):
        print()
        print("=" * 60)
        print("SUCCESS! Facebook session saved in Docker container.")
        print("The scripts will now use this session automatically.")
        print("=" * 60)
    else:
        print()
        print("=" * 60)
        print("WARNING: Could not verify Facebook login.")
        print("Please try again or check if you're logged in correctly.")
        print("=" * 60)

    driver.quit()
    print("\nDone!")


if __name__ == "__main__":
    main()
