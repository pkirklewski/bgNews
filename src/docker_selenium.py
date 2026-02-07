"""
Docker Selenium Helper Module for Boguszów-Gorce News

Provides functions to connect to Selenium running in Docker container.
This replaces the unreliable undetected_chromedriver with a stable,
isolated Chrome instance.

Usage:
    from docker_selenium import get_docker_driver, is_container_running, start_container
"""

import subprocess
import time
import logging
from pathlib import Path
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.common.exceptions import WebDriverException

logger = logging.getLogger(__name__)

# Configuration
PROJECT_ROOT = Path(__file__).parent.parent
DOCKER_COMPOSE_FILE = PROJECT_ROOT / "docker-compose.yml"
SELENIUM_URL = "http://localhost:4445/wd/hub"
CONTAINER_NAME = "bg-selenium-chrome"


def is_container_running() -> bool:
    """Check if the Selenium Docker container is running."""
    try:
        result = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", CONTAINER_NAME],
            capture_output=True, text=True, timeout=10
        )
        return result.stdout.strip() == "true"
    except Exception:
        return False


def is_selenium_ready() -> bool:
    """Check if Selenium WebDriver is ready to accept connections."""
    import urllib.request
    try:
        response = urllib.request.urlopen(f"{SELENIUM_URL}/status", timeout=5)
        return response.status == 200
    except Exception:
        return False


def start_container() -> bool:
    """Start the Selenium Docker container."""
    logger.info("Starting Selenium Docker container...")
    try:
        result = subprocess.run(
            ["docker", "compose", "-f", str(DOCKER_COMPOSE_FILE), "up", "-d"],
            capture_output=True, text=True, timeout=120,
            cwd=str(PROJECT_ROOT)
        )
        if result.returncode != 0:
            logger.error(f"Failed to start container: {result.stderr}")
            return False

        # Wait for Selenium to be ready
        for i in range(30):  # Wait up to 30 seconds
            if is_selenium_ready():
                logger.info("Selenium container is ready!")
                return True
            time.sleep(1)

        logger.error("Selenium container started but not responding")
        return False
    except Exception as e:
        logger.error(f"Error starting container: {e}")
        return False


def stop_container() -> bool:
    """Stop the Selenium Docker container."""
    logger.info("Stopping Selenium Docker container...")
    try:
        result = subprocess.run(
            ["docker", "compose", "-f", str(DOCKER_COMPOSE_FILE), "down"],
            capture_output=True, text=True, timeout=60,
            cwd=str(PROJECT_ROOT)
        )
        return result.returncode == 0
    except Exception as e:
        logger.error(f"Error stopping container: {e}")
        return False


def restart_container() -> bool:
    """Restart the Selenium Docker container."""
    logger.info("Restarting Selenium Docker container...")
    stop_container()
    time.sleep(2)
    return start_container()


def get_docker_driver(max_retries: int = 3):
    """
    Get a Selenium WebDriver connected to the Docker container.

    Automatically starts the container if not running.
    Returns a configured Chrome WebDriver.
    """
    for attempt in range(1, max_retries + 1):
        try:
            logger.info(f"Connecting to Docker Selenium (attempt {attempt}/{max_retries})...")

            # Ensure container is running
            if not is_container_running():
                logger.info("Container not running, starting it...")
                if not start_container():
                    raise RuntimeError("Failed to start Selenium container")
            elif not is_selenium_ready():
                logger.info("Container running but Selenium not ready, restarting...")
                if not restart_container():
                    raise RuntimeError("Failed to restart Selenium container")

            # Configure Chrome options
            options = Options()
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
            options.add_argument("--disable-gpu")
            options.add_argument("--window-size=1920,1080")
            options.add_argument("--disable-notifications")
            options.add_argument("--disable-infobars")
            # Use persistent profile directory (mounted from host)
            options.add_argument("--user-data-dir=/home/seluser/.config/google-chrome")

            # Anti-detection measures
            options.add_argument("--disable-blink-features=AutomationControlled")
            options.add_experimental_option("excludeSwitches", ["enable-automation"])
            options.add_experimental_option("useAutomationExtension", False)

            # Connect to remote Selenium
            driver = webdriver.Remote(
                command_executor=SELENIUM_URL,
                options=options
            )

            # Additional anti-detection via CDP
            driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
                "source": """
                    Object.defineProperty(navigator, 'webdriver', {
                        get: () => undefined
                    });
                """
            })

            logger.info("Connected to Docker Selenium successfully!")
            return driver

        except Exception as e:
            logger.warning(f"Attempt {attempt} failed: {e}")
            if attempt < max_retries:
                logger.info(f"Waiting 10s before retry...")
                # Try restarting the container
                restart_container()
                time.sleep(10)
            else:
                logger.error("All connection attempts failed")
                raise


def check_facebook_login(driver) -> bool:
    """Check if logged into Facebook."""
    try:
        driver.get("https://www.facebook.com")
        time.sleep(3)

        # Check for login form
        if 'login' in driver.current_url.lower():
            return False
        if len(driver.find_elements(By.NAME, "email")) > 0:
            return False
        return True
    except Exception:
        return False


def get_container_logs(lines: int = 50) -> str:
    """Get recent logs from the Selenium container."""
    try:
        result = subprocess.run(
            ["docker", "logs", "--tail", str(lines), CONTAINER_NAME],
            capture_output=True, text=True, timeout=10
        )
        return result.stdout + result.stderr
    except Exception as e:
        return f"Error getting logs: {e}"


def get_container_status() -> dict:
    """Get detailed status of the Selenium container."""
    status = {
        "container_running": is_container_running(),
        "selenium_ready": False,
        "container_name": CONTAINER_NAME,
    }

    if status["container_running"]:
        status["selenium_ready"] = is_selenium_ready()

        # Get container info
        try:
            result = subprocess.run(
                ["docker", "inspect", "--format",
                 '{"status":"{{.State.Status}}","started":"{{.State.StartedAt}}","health":"{{.State.Health.Status}}"}',
                 CONTAINER_NAME],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0:
                import json
                info = json.loads(result.stdout.strip())
                status.update(info)
        except Exception:
            pass

    return status
