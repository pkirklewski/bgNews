#!/usr/bin/env python3
"""
Boguszów-Gorce Weather MAP Generator & Selenium Auto-Poster
Generuje mapę temperatur dla dzielnic Boguszowa-Gorc i publikuje na FB przez Selenium.
"""

import requests
import logging
import os
import time
import random
from datetime import datetime
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.action_chains import ActionChains
from selenium.common.exceptions import TimeoutException, NoSuchElementException

# ============================================
# KONFIGURACJA
# ============================================

# Chrome paths (instead of Opera)
CHROME_BINARY = "/usr/bin/google-chrome"
CHROME_PROFILE = "/home/pkirklewski/.config/chrome-fb-bot"

# Facebook - Boguszów-Gorce Newsy i Informacje
FB_PAGE_URL = "https://www.facebook.com/profile.php?id=100027689516729"
FB_PROFILE_LINK = "fb.com/profile.php?id=100027689516729"

# Open-Meteo
OPENMETEO_URL = "https://api.open-meteo.com/v1/forecast"

# Paths
SCRIPT_DIR = Path(__file__).parent
INPUT_MAP_FILENAME = "map.png"
OUTPUT_IMAGE_FILENAME = "boguszow_gorce_temp_map_final.png"

# Logging
logging.basicConfig(
    level=logging.INFO, 
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ============================================
# LISTA DZIELNIC / MIEJSC
# ============================================

DISTRICTS = [
    {"name": "Lubominek",         "lat": 50.7750, "lon": 16.1900, "x": 385, "y": 235},
    {"name": "Chełmiec",          "lat": 50.7789, "lon": 16.2110, "x": 669, "y": 220},
    {"name": "Gorce",             "lat": 50.7600, "lon": 16.1950, "x": 154, "y": 490},
    {"name": "Boguszów-Gorce",    "lat": 50.7551, "lon": 16.2049, "x": 594, "y": 670},
    {"name": "Stary Lesieniec",   "lat": 50.7477, "lon": 16.1869, "x": 403, "y": 830},
    {"name": "Kuźnice Świdnickie","lat": 50.7469, "lon": 16.2204, "x": 750, "y": 890},
    {"name": "Dzikowiec",         "lat": 50.7245, "lon": 16.2195, "x": 665, "y": 1250},
]

# Index of the central/reference district for forecast (Boguszów-Gorce)
CENTER_DISTRICT_INDEX = 3

# ============================================
# HUMAN-LIKE HELPERS
# ============================================

def human_delay(min_sec: float = 0.5, max_sec: float = 2.0):
    """Random delay to mimic human behavior"""
    time.sleep(random.uniform(min_sec, max_sec))

def human_type(element, text: str, min_delay: float = 0.03, max_delay: float = 0.12):
    """Type text character by character like a human"""
    for char in text:
        element.send_keys(char)
        time.sleep(random.uniform(min_delay, max_delay))

def random_mouse_movement(driver):
    """Simulate random mouse movements"""
    action = ActionChains(driver)
    for _ in range(random.randint(1, 3)):
        x_offset = random.randint(-100, 100)
        y_offset = random.randint(-100, 100)
        action.move_by_offset(x_offset, y_offset)
        human_delay(0.1, 0.3)
    try:
        action.perform()
    except:
        pass

# ============================================
# FONT & COLOR HELPERS
# ============================================

def get_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ] if bold else [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    
    for p in paths:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except:
                continue
    return ImageFont.load_default()

def get_temp_color(temp: float) -> tuple:
    if temp <= -10: return (100, 180, 255)
    elif temp < 0:  return (50, 150, 255)
    elif temp == 0: return (220, 220, 220)
    elif temp < 10: return (255, 200, 100)
    elif temp < 25: return (255, 140, 50)
    return (255, 80, 80)

# ============================================
# WEATHER DATA FETCHING
# ============================================

def fetch_with_retry(url: str, params: dict, max_retries: int = 3) -> dict:
    """Fetch with exponential backoff retry logic"""
    for attempt in range(max_retries):
        try:
            timeout = 30 + (attempt * 15)
            logger.info(f"Attempt {attempt + 1}/{max_retries}, timeout={timeout}s")
            r = requests.get(url, params=params, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except requests.exceptions.Timeout:
            logger.warning(f"Timeout on attempt {attempt + 1}")
            if attempt < max_retries - 1:
                wait = 5 * (attempt + 1)
                logger.info(f"Waiting {wait}s before retry...")
                time.sleep(wait)
        except requests.exceptions.RequestException as e:
            logger.error(f"Request error: {e}")
            if attempt < max_retries - 1:
                time.sleep(5)
    return None

def fetch_single_district(district: dict) -> dict:
    """Fetch weather for a single district"""
    params = {
        "latitude": district['lat'],
        "longitude": district['lon'],
        "current": ["temperature_2m", "weather_code"],
        "timezone": "Europe/Warsaw"
    }
    
    data = fetch_with_retry(OPENMETEO_URL, params, max_retries=2)
    
    if data:
        current = data.get('current', {})
        result = district.copy()
        result['temp'] = current.get('temperature_2m', 0)
        result['code'] = current.get('weather_code', 0)
        return result
    return None

def fetch_districts_weather() -> list:
    """Fetch weather - try batch first, fallback to individual"""
    
    lats = [d['lat'] for d in DISTRICTS]
    lons = [d['lon'] for d in DISTRICTS]
    
    params = {
        "latitude": lats,
        "longitude": lons,
        "current": ["temperature_2m", "weather_code"],
        "timezone": "Europe/Warsaw"
    }
    
    logger.info("Trying batch request...")
    data = fetch_with_retry(OPENMETEO_URL, params, max_retries=2)
    
    if data:
        results = []
        weather_list = data if isinstance(data, list) else [data]
        
        for i, station_data in enumerate(weather_list):
            current = station_data.get('current', {})
            district_info = DISTRICTS[i].copy()
            district_info['temp'] = current.get('temperature_2m', 0)
            district_info['code'] = current.get('weather_code', 0)
            results.append(district_info)
        
        logger.info(f"✅ Batch request succeeded: {len(results)} districts")
        return results
    
    # Fallback - fetch individually
    logger.warning("Batch failed, fetching districts individually...")
    results = []
    
    for i, district in enumerate(DISTRICTS):
        logger.info(f"Fetching {district['name']} ({i+1}/{len(DISTRICTS)})...")
        result = fetch_single_district(district)
        
        if result:
            results.append(result)
        else:
            logger.warning(f"Failed to fetch {district['name']}, using fallback")
            fallback = district.copy()
            fallback['temp'] = 0
            fallback['code'] = 3
            results.append(fallback)
        
        time.sleep(0.3)
    
    if len([r for r in results if r.get('temp', 0) != 0]) > 0:
        logger.info(f"✅ Individual fetch completed: {len(results)} districts")
        return results
    
    logger.error("❌ All fetch methods failed")
    return []

def fetch_forecast_center() -> dict:
    """Fetch hourly forecast for Boguszów-Gorce center for today"""
    
    # Boguszów-Gorce center coordinates
    lat = DISTRICTS[CENTER_DISTRICT_INDEX]['lat']
    lon = DISTRICTS[CENTER_DISTRICT_INDEX]['lon']
    
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": ["temperature_2m", "precipitation_probability", "weather_code"],
        "timezone": "Europe/Warsaw",
        "forecast_days": 2  # Need 2 days to cover "tonight"
    }
    
    logger.info("Fetching forecast for Boguszów-Gorce...")
    data = fetch_with_retry(OPENMETEO_URL, params, max_retries=2)
    
    if not data:
        logger.warning("Could not fetch forecast")
        return None
    
    hourly = data.get('hourly', {})
    times = hourly.get('time', [])
    temps = hourly.get('temperature_2m', [])
    precip_probs = hourly.get('precipitation_probability', [])
    weather_codes = hourly.get('weather_code', [])
    
    if not times or not temps:
        logger.warning("No hourly data in response")
        return None
    
    # Get current hour
    from datetime import datetime
    now = datetime.now()
    current_hour = now.hour
    
    # Find indices for day (6:00-18:00) and night (18:00-6:00 next day)
    day_temps = []
    night_temps = []
    day_precip = []
    night_precip = []
    day_codes = []
    night_codes = []
    
    for i, time_str in enumerate(times):
        hour = int(time_str[11:13])  # Extract hour from "2026-01-06T14:00"
        day = int(time_str[8:10])    # Extract day
        
        # Today's daytime (6:00-18:00)
        if day == now.day and 6 <= hour < 18:
            day_temps.append(temps[i])
            day_precip.append(precip_probs[i] if i < len(precip_probs) else 0)
            day_codes.append(weather_codes[i] if i < len(weather_codes) else 0)
        
        # Tonight (18:00 today to 6:00 tomorrow)
        elif (day == now.day and hour >= 18) or (day == now.day + 1 and hour < 6):
            night_temps.append(temps[i])
            night_precip.append(precip_probs[i] if i < len(precip_probs) else 0)
            night_codes.append(weather_codes[i] if i < len(weather_codes) else 0)
    
    result = {
        "day_max": round(max(day_temps)) if day_temps else None,
        "day_min": round(min(day_temps)) if day_temps else None,
        "night_min": round(min(night_temps)) if night_temps else None,
        "day_precip_max": max(day_precip) if day_precip else 0,
        "night_precip_max": max(night_precip) if night_precip else 0,
        "day_codes": day_codes,
        "night_codes": night_codes
    }
    
    logger.info(f"✅ Forecast: day max {result['day_max']}°C, night min {result['night_min']}°C")
    return result

def generate_forecast_text(forecast: dict) -> str:
    """Generate 2-sentence forecast text"""
    
    if not forecast:
        return "Sprawdź temperaturę w swojej okolicy na mapie"
    
    sentences = []
    
    # Sentence 1: Day max temperature
    day_max = forecast.get('day_max')
    if day_max is not None:
        sentences.append(f"Dziś maksymalnie {day_max:+d}°C")
    
    # Add night min
    night_min = forecast.get('night_min')
    if night_min is not None:
        if sentences:
            sentences[0] += f", w nocy spadek do {night_min:+d}°C."
        else:
            sentences.append(f"W nocy temperatura spadnie do {night_min:+d}°C.")
    elif sentences:
        sentences[0] += "."
    
    # Sentence 2: Conditions (clouds/precipitation)
    day_precip = forecast.get('day_precip_max', 0)
    night_precip = forecast.get('night_precip_max', 0)
    day_codes = forecast.get('day_codes', [])
    
    # Determine sky conditions from weather codes
    # 0-1: clear, 2-3: cloudy, 45-48: fog, 51-67: rain, 71-86: snow
    avg_code = sum(day_codes) / len(day_codes) if day_codes else 3
    
    if avg_code <= 1:
        sky = "Bezchmurnie"
    elif avg_code <= 3:
        sky = "Zachmurzenie umiarkowane"
    elif avg_code <= 48:
        sky = "Możliwe mgły"
    elif avg_code <= 67:
        sky = "Zachmurzenie z opadami deszczu"
    elif avg_code <= 86:
        sky = "Zachmurzenie z opadami śniegu"
    else:
        sky = "Pochmurno"
    
    # Precipitation info
    if day_precip > 60 or night_precip > 60:
        if avg_code >= 71:
            precip_text = "możliwe opady śniegu"
        else:
            precip_text = "możliwe opady"
        sentences.append(f"{sky}, {precip_text}.")
    elif day_precip > 30 or night_precip > 30:
        sentences.append(f"{sky}, niewielkie szanse opadów.")
    else:
        sentences.append(f"{sky} bez opadów.")
    
    return " ".join(sentences)

# ============================================
# MAP IMAGE GENERATION
# ============================================

def draw_text_centered(draw, x, y, text, font, color, stroke_width=3, stroke_fill=(0,0,0)):
    bbox = draw.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    
    pos_x = x - text_w // 2
    pos_y = y - text_h // 2
    
    draw.text((pos_x, pos_y), text, font=font, fill=color, 
              stroke_width=stroke_width, stroke_fill=stroke_fill)

def generate_map_image(districts_data: list) -> tuple:
    input_path = SCRIPT_DIR / INPUT_MAP_FILENAME
    output_path = SCRIPT_DIR / OUTPUT_IMAGE_FILENAME
    
    if not input_path.exists():
        logger.error(f"❌ Brak pliku mapy: {input_path}")
        return None, 0, 0

    try:
        img = Image.open(input_path).convert('RGBA')
        draw = ImageDraw.Draw(img)
        
        font_temp = get_font(55, bold=True)
        font_info = get_font(24, bold=True)

        min_temp = 100
        max_temp = -100

        for d in districts_data:
            # Skip if x,y are 0 (not configured yet)
            if d['x'] == 0 and d['y'] == 0:
                logger.warning(f"⚠️ Skipping {d['name']} - X,Y not configured")
                continue
                
            temp = round(d['temp'])
            if temp < min_temp: min_temp = temp
            if temp > max_temp: max_temp = temp

            temp_str = f"{temp:+d}°" if temp != 0 else "0°"
            color = get_temp_color(temp)
            
            draw_text_centered(draw, d['x'], d['y'], temp_str, font_temp, color)

        now_str = datetime.now().strftime("%d.%m.%Y godz. %H:%M")
        footer_text = f"Stan na: {now_str} | Dane: Open-Meteo"
        
        w, h = img.size
        draw.text((w - 450, h - 40), footer_text, font=font_info, fill=(0, 0, 0))

        img.save(output_path, "PNG")
        logger.info(f"✅ Mapa wygenerowana: {output_path}")
        
        return str(output_path), min_temp, max_temp
        
    except Exception as e:
        logger.error(f"❌ Błąd generowania mapy: {e}")
        return None, 0, 0

# ============================================
# SELENIUM FACEBOOK POSTING
# ============================================

def setup_chrome_driver():
    """Configure and return undetected Chrome driver with dedicated profile"""
    
    options = uc.ChromeOptions()
    
    # Use dedicated Chrome profile for FB automation
    options.add_argument(f"--user-data-dir={CHROME_PROFILE}")
    
    # Set Chrome binary
    options.binary_location = CHROME_BINARY
    
    # Clean start
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_argument("--disable-infobars")
    
    # Disable notifications
    options.add_argument("--disable-notifications")
    
    # Create undetected driver
    driver = uc.Chrome(
        options=options,
        use_subprocess=True
    )
    
    # Set window size and position AFTER creation (uc ignores args)
    driver.set_window_size(1920,1080) 
    driver.set_window_position(0, 0)
    
    return driver

def post_to_facebook_selenium(image_path: str, caption: str) -> bool:
    """Post image with caption to Facebook using Selenium"""
    
    driver = None
    try:
        logger.info("🚀 Starting Chrome browser...")
        driver = setup_chrome_driver()
        
        # Navigate to Facebook page
        logger.info(f"📍 Navigating to {FB_PAGE_URL}")
        driver.get(FB_PAGE_URL)
        human_delay(4, 5)
        
        # ============================================
        # HANDLE COOKIE CONSENT POPUP
        # ============================================
        
        logger.info("🍪 Checking for cookie consent popup...")
        
        cookie_handled = False
        
        # Method 1: Direct click using data-testid (Facebook specific)
        try:
            cookie_btn = WebDriverWait(driver, 5).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, "[data-testid='cookie-policy-manage-dialog-accept-button']"))
            )
            cookie_btn.click()
            cookie_handled = True
            logger.info("✅ Clicked cookie button via data-testid")
        except:
            pass
        
        # Method 2: Try aria-label
        if not cookie_handled:
            try:
                cookie_btn = WebDriverWait(driver, 3).until(
                    EC.element_to_be_clickable((By.XPATH, "//button[@aria-label='Allow all cookies']"))
                )
                cookie_btn.click()
                cookie_handled = True
                logger.info("✅ Clicked cookie button via aria-label")
            except:
                pass
        
        # Method 3: Click by coordinates (fallback - button is usually bottom right of popup)
        if not cookie_handled:
            try:
                # Find any visible dialog/popup
                dialog = driver.find_element(By.XPATH, "//div[contains(@class, 'x1n2onr6') and contains(@class, 'x1ja2u2z')]")
                if dialog:
                    # Use ActionChains to click the "Allow all cookies" area
                    from selenium.webdriver.common.action_chains import ActionChains
                    actions = ActionChains(driver)
                    # The button is typically in the lower right area of the popup
                    actions.move_to_element_with_offset(dialog, 150, 200).click().perform()
                    logger.info("✅ Clicked cookie area via coordinates")
                    cookie_handled = True
            except:
                pass
        
        # Method 4: Pure JavaScript with retries
        if not cookie_handled:
            for _ in range(3):
                try:
                    result = driver.execute_script("""
                        // Try to find and click the button
                        const selectors = [
                            'button[data-testid="cookie-policy-manage-dialog-accept-button"]',
                            'button[aria-label="Allow all cookies"]',
                            'button[aria-label="Zezwól na wszystkie pliki cookie"]',
                        ];
                        
                        for (let sel of selectors) {
                            const btn = document.querySelector(sel);
                            if (btn) {
                                btn.click();
                                return 'clicked: ' + sel;
                            }
                        }
                        
                        // Fallback: find by text content
                        const allButtons = Array.from(document.querySelectorAll('button'));
                        for (let btn of allButtons) {
                            if (btn.textContent.includes('Allow all') || 
                                btn.textContent.includes('Zezwól na wszystkie')) {
                                btn.click();
                                return 'clicked by text';
                            }
                        }
                        
                        return 'not found';
                    """)
                    if 'clicked' in str(result):
                        logger.info(f"✅ {result}")
                        cookie_handled = True
                        break
                except:
                    pass
                human_delay(0.5, 1)
        
        if cookie_handled:
            human_delay(2, 3)
        else:
            logger.warning("⚠️ Cookie popup not handled - may need manual intervention once")
            # Save screenshot for debugging
            driver.save_screenshot(str(SCRIPT_DIR / "debug_cookie_popup.png"))
        
        human_delay(2, 3)
        
        # ============================================
        # STEP 1: SWITCH TO PAGE PROFILE
        # ============================================
        
        logger.info("🔄 Looking for 'Switch to page' button...")
        
        switched = False
        
        # Step 1a: Click "Przełącz teraz" button on the page
        switch_now_selectors = [
            "//span[text()='Przełącz teraz']",
            "//div[@role='button']//span[text()='Przełącz teraz']",
            "//div[contains(@class, 'x1i10hfl')]//span[text()='Przełącz teraz']",
        ]
        
        for selector in switch_now_selectors:
            try:
                btn = WebDriverWait(driver, 5).until(
                    EC.element_to_be_clickable((By.XPATH, selector))
                )
                if btn:
                    logger.info(f"✅ Found 'Przełącz teraz': {selector}")
                    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", btn)
                    human_delay(0.5, 1)
                    btn.click()
                    logger.info("✅ Clicked 'Przełącz teraz'")
                    human_delay(2, 3)
                    break
            except:
                continue
        
        # Step 1b: Handle the confirmation dialog - click "Przełącz" in the modal
        logger.info("🔄 Looking for confirmation dialog...")
        human_delay(1, 2)
        
        confirm_selectors = [
            "//div[@role='dialog']//span[text()='Przełącz']",
            "//div[@role='dialog']//div[@role='button']//span[text()='Przełącz']",
            "//div[contains(@aria-label, 'Przełącz')]//span[text()='Przełącz']",
            "//span[text()='Przełącz']/ancestor::div[@role='button']",
        ]
        
        for selector in confirm_selectors:
            try:
                btn = WebDriverWait(driver, 5).until(
                    EC.element_to_be_clickable((By.XPATH, selector))
                )
                if btn:
                    logger.info(f"✅ Found confirm button: {selector}")
                    btn.click()
                    logger.info("✅ Clicked 'Przełącz' in dialog!")
                    switched = True
                    human_delay(3, 5)
                    break
            except:
                continue
        
        # Step 1c: Also try the left sidebar "Przełącz" button
        if not switched:
            try:
                sidebar_btn = driver.find_element(By.XPATH, "//div[contains(@class, 'x1iyjqo2')]//span[text()='Przełącz']")
                if sidebar_btn:
                    sidebar_btn.click()
                    logger.info("✅ Clicked sidebar 'Przełącz'")
                    human_delay(2, 3)
                    # Now look for dialog again
                    for selector in confirm_selectors:
                        try:
                            btn = WebDriverWait(driver, 5).until(
                                EC.element_to_be_clickable((By.XPATH, selector))
                            )
                            if btn:
                                btn.click()
                                switched = True
                                human_delay(3, 5)
                                break
                        except:
                            continue
            except:
                pass
        
        if not switched:
            logger.warning("⚠️ Could not complete profile switch - continuing anyway")
            driver.save_screenshot(str(SCRIPT_DIR / "debug_no_switch.png"))
        else:
            logger.info("✅ Successfully switched to page profile!")
        
        human_delay(2, 3)
        
        # ============================================
        # STEP 2: FIND AND CLICK "CO SŁYCHAĆ?" INPUT
        # ============================================
        
        logger.info("🔍 Looking for post creation area...")
        
        # Scroll to top first
        driver.execute_script("window.scrollTo(0, 0);")
        human_delay(1, 2)
        
        post_box_found = False
        
        # Use precise Selenium selectors only
        post_box_selectors = [
            (By.XPATH, "//span[text()='Co słychać?']"),
            (By.XPATH, "//span[contains(text(), 'Co słychać')]"),
            (By.XPATH, "//div[@role='button']//span[text()='Co słychać?']"),
            (By.XPATH, "//span[contains(text(), \"What's on your mind\")]"),
            (By.XPATH, "//div[contains(@aria-label, 'Utwórz post')]"),
            (By.XPATH, "//div[contains(@aria-label, 'Create a post')]"),
            (By.XPATH, "//div[@data-pagelet='ProfileComposer']//div[@role='button']"),
        ]
        
        for by, selector in post_box_selectors:
            try:
                post_box = WebDriverWait(driver, 5).until(
                    EC.element_to_be_clickable((by, selector))
                )
                if post_box:
                    logger.info(f"✅ Found post box: {selector}")
                    # Make sure we're clicking the right element
                    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", post_box)
                    human_delay(0.5, 1)
                    
                    # Log position for debugging
                    location = post_box.location
                    size = post_box.size
                    logger.info(f"📍 Element position: x={location['x']}, y={location['y']}, w={size['width']}, h={size['height']}")
                    
                    post_box.click()
                    post_box_found = True
                    logger.info("✅ Clicked post creation area")
                    human_delay(3, 4)
                    break
            except Exception as e:
                logger.debug(f"Selector failed: {selector} - {e}")
                continue
        
        if not post_box_found:
            logger.error("❌ Could not find 'Co słychać?' post creation area")
            driver.save_screenshot(str(SCRIPT_DIR / "debug_no_post_box.png"))
            return False
        
        logger.info("✅ Clicked post creation area")
        
        # ============================================
        # STEP 3: WAIT FOR POST DIALOG MODAL
        # ============================================
        
        logger.info("⏳ Waiting for post dialog to open...")
        
        try:
            WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.XPATH, "//div[@role='dialog']"))
            )
            logger.info("✅ Post dialog opened")
        except:
            logger.warning("⚠️ Dialog may not have opened properly")
        
        human_delay(2, 3)
        
        # ============================================
        # STEP 4: UPLOAD IMAGE (directly via hidden input)
        # ============================================
        
        logger.info(f"📤 Uploading image: {image_path}")
        
        # Find all file inputs
        file_inputs = driver.find_elements(By.XPATH, "//input[@type='file']")
        logger.info(f"Found {len(file_inputs)} file input(s)")
        
        # Log details of each input for debugging
        for i, fi in enumerate(file_inputs):
            accept = fi.get_attribute('accept') or 'none'
            multiple = fi.get_attribute('multiple') or 'false'
            logger.info(f"  Input #{i+1}: accept='{accept[:50]}...', multiple={multiple}")
        
        # Strategy: prefer input with multiple=true (usually the post dialog one)
        # or find input inside the dialog
        uploaded = False
        
        # First try: input inside the dialog
        try:
            dialog_input = driver.find_element(By.XPATH, "//div[@role='dialog']//input[@type='file']")
            dialog_input.send_keys(image_path)
            logger.info("✅ Image sent to dialog file input")
            uploaded = True
        except Exception as e:
            logger.info(f"No dialog input found: {e}")
        
        # Second try: input with multiple=true and image/* accept
        if not uploaded:
            for i, file_input in enumerate(file_inputs):
                try:
                    accept = file_input.get_attribute('accept') or ''
                    multiple = file_input.get_attribute('multiple')
                    
                    # Prefer input with multiple=true that accepts images
                    if multiple and 'image' in accept:
                        file_input.send_keys(image_path)
                        logger.info(f"✅ Image sent to file input #{i+1} (multiple=true)")
                        uploaded = True
                        break
                except Exception as e:
                    logger.warning(f"File input #{i+1} failed: {e}")
                    continue
        
        # Third try: any input that accepts images
        if not uploaded:
            for i, file_input in enumerate(file_inputs):
                try:
                    accept = file_input.get_attribute('accept') or ''
                    if 'image' in accept:
                        file_input.send_keys(image_path)
                        logger.info(f"✅ Image sent to file input #{i+1}")
                        uploaded = True
                        break
                except Exception as e:
                    logger.warning(f"File input #{i+1} failed: {e}")
                    continue
        
        if not uploaded:
            logger.error("❌ Could not upload image via any file input")
            driver.save_screenshot(str(SCRIPT_DIR / "debug_no_upload.png"))
            return False
        
        # Wait for image to process and appear in dialog
        logger.info("⏳ Waiting for image to process...")
        human_delay(6, 8)
        
        # Take screenshot to see if image appeared
        driver.save_screenshot(str(SCRIPT_DIR / "debug_after_upload.png"))
        
        # Verify image appeared - look for blob: src which indicates uploaded file
        try:
            img_preview = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.XPATH, "//div[@role='dialog']//img[contains(@src, 'blob:')]"))
            )
            logger.info("✅ Uploaded image (blob:) visible in dialog")
        except:
            # Maybe it's already uploaded to FB servers
            try:
                img_preview = driver.find_element(By.XPATH, "//div[@role='dialog']//img[contains(@class, 'x1ey2m1c')]")
                logger.info("✅ Image visible in dialog (FB class)")
            except:
                logger.warning("⚠️ Could not verify image in dialog - check debug_after_upload.png")
                # Don't return False - continue and see what happens
        
        # ============================================
        # STEP 5: ENTER CAPTION TEXT
        # ============================================
        
        logger.info("📝 Entering caption...")
        
        text_area_selectors = [
            (By.XPATH, "//div[@role='dialog']//div[@role='textbox'][@contenteditable='true']"),
            (By.XPATH, "//div[@role='dialog']//div[@contenteditable='true']"),
            (By.CSS_SELECTOR, "div[role='dialog'] div[role='textbox'][contenteditable='true']"),
            (By.CSS_SELECTOR, "div[role='dialog'] [contenteditable='true']"),
        ]
        
        text_area = None
        for by, selector in text_area_selectors:
            try:
                elements = driver.find_elements(by, selector)
                for elem in elements:
                    if elem.is_displayed():
                        text_area = elem
                        logger.info(f"✅ Found text area: {selector}")
                        break
                if text_area:
                    break
            except:
                continue
        
        if text_area:
            text_area.click()
            human_delay(0.5, 1)
            
            # Type text line by line with Shift+Enter for breaks
            lines = caption.split('\n')
            for i, line in enumerate(lines):
                if line:
                    # Type line slowly to avoid issues
                    actions = ActionChains(driver)
                    actions.send_keys(line)
                    actions.perform()
                    human_delay(0.2, 0.3)
                
                if i < len(lines) - 1:
                    # Shift+Enter for line break
                    actions = ActionChains(driver)
                    actions.key_down(Keys.SHIFT).send_keys(Keys.ENTER).key_up(Keys.SHIFT)
                    actions.perform()
                    human_delay(0.1, 0.2)
            
            logger.info("✅ Caption entered line by line")
            
            human_delay(1, 2)
        else:
            logger.warning("⚠️ Could not find text area")
            driver.save_screenshot(str(SCRIPT_DIR / "debug_no_textarea.png"))
        
        # ============================================
        # STEP 6: CLICK PUBLISH BUTTON (2-step: Dalej -> Opublikuj)
        # ============================================
        
        logger.info("🚀 Looking for publish button...")
        human_delay(2, 3)
        
        driver.save_screenshot(str(SCRIPT_DIR / "debug_before_publish.png"))
        
        publish_selectors = [
            # "Dalej" / "Next" button (shown when link preview is detected)
            (By.XPATH, "//div[@role='dialog']//span[text()='Dalej']"),
            (By.XPATH, "//span[text()='Dalej']"),
            (By.XPATH, "//div[@role='dialog']//span[text()='Next']"),
            # Polish - Opublikuj
            (By.XPATH, "//div[@role='dialog']//span[text()='Opublikuj']"),
            (By.XPATH, "//div[@role='dialog']//div[@aria-label='Opublikuj']"),
            (By.XPATH, "//span[text()='Opublikuj']/ancestor::div[@role='button']"),
            # English
            (By.XPATH, "//div[@role='dialog']//span[text()='Post']"),
            (By.XPATH, "//div[@role='dialog']//div[@aria-label='Post']"),
        ]
        
        publish_btn = None
        for by, selector in publish_selectors:
            try:
                publish_btn = WebDriverWait(driver, 5).until(
                    EC.element_to_be_clickable((by, selector))
                )
                if publish_btn:
                    logger.info(f"✅ Found button: {selector}")
                    break
            except:
                continue
        
        if publish_btn:
            human_delay(0.5, 1)
            
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", publish_btn)
            human_delay(0.3, 0.5)
            
            publish_btn.click()
            logger.info("✅ Clicked first button (Dalej/Opublikuj)!")
            
            # Wait and check if there's a second step (Opublikuj after Dalej)
            human_delay(3, 4)
            
            # Look for final "Opublikuj" button if we clicked "Dalej"
            final_publish_selectors = [
                (By.XPATH, "//div[@role='dialog']//span[text()='Opublikuj']"),
                (By.XPATH, "//div[@role='dialog']//div[@aria-label='Opublikuj']"),
                (By.XPATH, "//span[text()='Opublikuj']"),
                (By.XPATH, "//div[@role='dialog']//span[text()='Post']"),
            ]
            
            for by, selector in final_publish_selectors:
                try:
                    final_btn = WebDriverWait(driver, 5).until(
                        EC.element_to_be_clickable((by, selector))
                    )
                    if final_btn:
                        logger.info(f"✅ Found final publish button: {selector}")
                        human_delay(0.5, 1)
                        final_btn.click()
                        logger.info("✅ Clicked final Opublikuj!")
                        break
                except:
                    continue
            
            # Handle any post-publish popups
            human_delay(2, 3)
            
            # Handle "Rozmawiaj bezpośrednio z ludźmi" popup - click "Nie teraz"
            popup_handled = False
            popup_selectors = [
                "//span[text()='Nie teraz']",
                "//div[@role='button']//span[text()='Nie teraz']",
                "//span[text()='Not Now']",
                "//span[text()='Pomiń']",
                "//span[text()='Skip']",
            ]
            
            for selector in popup_selectors:
                try:
                    popup_btn = WebDriverWait(driver, 3).until(
                        EC.element_to_be_clickable((By.XPATH, selector))
                    )
                    if popup_btn:
                        popup_btn.click()
                        logger.info(f"✅ Dismissed popup: {selector}")
                        popup_handled = True
                        human_delay(1, 2)
                        break
                except:
                    continue
            
            if not popup_handled:
                logger.info("ℹ️ No post-publish popup found (or already dismissed)")
            
            # Wait for post to complete
            human_delay(4, 6)
            
            # Check if dialog closed (success indicator)
            try:
                WebDriverWait(driver, 10).until_not(
                    EC.presence_of_element_located((By.XPATH, "//div[@role='dialog'][.//span[text()='Utwórz post']]"))
                )
                logger.info("✅ Post dialog closed!")
            except:
                # Maybe another popup appeared
                logger.info("ℹ️ Dialog still visible - checking for additional popups...")
                for selector in popup_selectors:
                    try:
                        popup_btn = driver.find_element(By.XPATH, selector)
                        popup_btn.click()
                        logger.info(f"✅ Dismissed additional popup: {selector}")
                        human_delay(2, 3)
                        break
                    except:
                        continue
            
            driver.save_screenshot(str(SCRIPT_DIR / "debug_after_publish.png"))
            
            # Verify post was published by refreshing and checking
            human_delay(2, 3)
            driver.refresh()
            human_delay(3, 4)
            
            # Look for our post text on the page
            try:
                post_indicator = driver.find_element(By.XPATH, "//div[contains(text(), 'Aktualna temperatura')]")
                logger.info("✅ Post verified on page!")
            except:
                logger.warning("⚠️ Could not verify post on page - check manually")
            
            logger.info("✅ Post published successfully!")
            return True
        else:
            logger.error("❌ Could not find publish button")
            driver.save_screenshot(str(SCRIPT_DIR / "debug_no_publish.png"))
            return False
            
    except Exception as e:
        logger.error(f"❌ Selenium error: {e}")
        import traceback
        traceback.print_exc()
        if driver:
            driver.save_screenshot(str(SCRIPT_DIR / "debug_error.png"))
        return False
        
    finally:
        if driver:
            human_delay(2, 3)
            logger.info("Closing browser...")
            driver.quit()

# ============================================
# MAIN
# ============================================

def main():
    logger.info(">>> Rozpoczynam generowanie mapy pogodowej dla Boguszowa-Gorc (Selenium)...")
    
    # 1. Fetch weather data
    districts_weather = fetch_districts_weather()
    if not districts_weather:
        logger.error("Brak danych pogodowych. Przerywam.")
        return

    # 2. Fetch forecast for center (Boguszów-Gorce)
    forecast = fetch_forecast_center()
    forecast_text = generate_forecast_text(forecast)

    # 3. Generate map image
    map_path, min_t, max_t = generate_map_image(districts_weather)
    
    if not map_path:
        logger.error("Nie udało się wygenerować mapy. Przerywam.")
        return
    
    #return #remove after testing the map 

    # 4. Prepare caption
    weather_code = districts_weather[CENTER_DISTRICT_INDEX]['code']  # Boguszów-Gorce
    
    desc = "Pochmurno"
    if weather_code in [0, 1]: desc = "Pogodnie"
    elif weather_code in [2, 3]: desc = "Pochmurno"
    elif weather_code in [45, 48]: desc = "Mgliście"
    elif weather_code >= 51 and weather_code <= 67: desc = "Opady deszczu"
    elif weather_code >= 71 and weather_code <= 86: desc = "Opady śniegu"
    
    range_str = f"{min_t:+d}°C" if min_t == max_t else f"od {min_t:+d}°C do {max_t:+d}°C"
    
    caption = f"""🌡 Aktualna temperatura w Boguszowie-Gorcach: {range_str}. {desc}.
{forecast_text}

Więcej: {FB_PROFILE_LINK}

👇#BoguszówGorce #BoguszówGorcePogoda #DolnyŚląsk #GóryKamienne"""

    logger.info(f"Treść posta:\n{caption}")

    # 5. Post to Facebook using Selenium
    success = post_to_facebook_selenium(map_path, caption)
    
    if success:
        logger.info("🎉 Post opublikowany pomyślnie!")
    else:
        logger.error("❌ Nie udało się opublikować posta")

if __name__ == "__main__":
    main() 