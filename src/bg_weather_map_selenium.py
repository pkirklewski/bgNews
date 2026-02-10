#!/usr/bin/env python3
"""
Boguszów-Gorce Weather MAP Generator & Selenium Auto-Poster
Generuje mapę temperatur dla dzielnic Boguszowa-Gorc i publikuje na FB przez Selenium.
Wybiera odpowiednią mapę (z ikoną pogody) na podstawie kodu WMO.

================================================================================
HISTORIA WERSJI:
================================================================================
v1.0.0  2026-02-07  Pierwsza wersja - adaptacja skryptu Wałbrzycha dla Boguszowa-Gorc
                    - 7 dzielnic Boguszowa-Gorc
                    - Docker Selenium (always)
                    - Brak udostępniania do grup
                    - Dedykowana strona FB: Boguszów-Gorce Newsy i Informacje
================================================================================

CRON (z USE_VIRTUAL_DISPLAY=True w skrypcie):
0 6,18 * * * /home/pkirklewski/scripts/bgnews/venv/bin/python /home/pkirklewski/scripts/bgnews/src/bg_weather_map_selenium.py >> /home/pkirklewski/scripts/bgnews/logs/cron.log 2>&1

================================================================================
"""

import requests
import logging
import os
import sys
import time
import random
import subprocess
import fcntl
import atexit
import signal
from datetime import datetime
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageFilter

from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.action_chains import ActionChains
from selenium.common.exceptions import TimeoutException, NoSuchElementException, SessionNotCreatedException

# ============================================
# KONFIGURACJA
# ============================================

# TEST MODE - True = testuj cały pipeline, ale nie publikuj na końcu
TEST_MODE = False

# USE VIRTUAL DISPLAY - True = xvfb (dla crona), False = normalna przeglądarka
USE_VIRTUAL_DISPLAY = False

# Docker Selenium - ALWAYS TRUE FOR BOGUSZÓW-GORCE
USE_DOCKER = True

# Facebook
FB_PAGE_URL = "https://www.facebook.com/profile.php?id=100027689516729"
FB_PAGE_NAME = "Boguszów-Gorce Newsy i Informacje"
FB_PROFILE_LINK = "fb.com/profile.php?id=100027689516729"

# Open-Meteo
OPENMETEO_URL = "https://api.open-meteo.com/v1/forecast"

# Paths
PROJECT_ROOT = Path(__file__).parent.parent
MAPS_DIR = PROJECT_ROOT / "assets" / "weather_maps"
OUTPUT_IMAGE_FILENAME = "boguszow_gorce_temp_map_final.png"

# Process isolation & locking
LOCK_FILE = PROJECT_ROOT / "locks" / "weather_map.lock"

# Group sharing - share weather map to local Facebook groups after posting to page
SHARE_TO_GROUPS_ENABLED = True
SHARE_TO_GROUPS = [
    "BOGUSZÓW-GORCE",               # "BOGUSZÓW-GORCE/Ogłoszenia/Informacje/Sprzedam/Kupię/Zamienię/"
    "Ogłoszenia Boguszów-Gorce",     # "Ogłoszenia Boguszów-Gorce"
    "Społeczność Kuźnic",            # "Społeczność Kuźnic"
]
SHARE_DELAY_SECONDS = 15  # Delay between group shares to avoid rate limiting
PERSONAL_PROFILE_NAME = "Piotr Kirklewski"

# ============================================
# CHARITY OVERLAY CONFIGURATION
# ============================================
# Overlay image (1.5% tax donation advertisement)
OVERLAY_ENABLED = True
OVERLAY_IMAGE = MAPS_DIR / "1_5_percentMapOverlayImageTranspartenBCKG.png"
OVERLAY_POSITION = (900, 1090)  # Bottom-right area, moved 30px up
OVERLAY_SHADOW_ENABLED = True
OVERLAY_SHADOW_OFFSET = 10  # Same as weather icon shadow
OVERLAY_SHADOW_COLOR = (160, 160, 160, 60)  # Light grey, same as weather icon shadow
OVERLAY_SHADOW_BLUR = 12  # Wide dispersed blur, same as weather icon shadow

# Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
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
        logger.info("🔒 Script lock acquired")
        return True
    except (IOError, OSError) as e:
        if _lock_file_handle:
            _lock_file_handle.close()
            _lock_file_handle = None
        logger.error(f"❌ Could not acquire lock - another instance may be running: {e}")
        return False


def release_script_lock():
    """Release the script lock."""
    global _lock_file_handle

    if _lock_file_handle:
        try:
            fcntl.flock(_lock_file_handle.fileno(), fcntl.LOCK_UN)
            _lock_file_handle.close()
            _lock_file_handle = None
            logger.info("🔓 Script lock released")
        except Exception as e:
            logger.warning(f"⚠️ Error releasing lock: {e}")


# ============================================
# MAPOWANIE KODÓW WMO NA PLIKI MAP
# ============================================

def get_map_for_code(code: int, mode: str = "day") -> str:
    """
    Zwraca nazwę pliku mapy na podstawie kodu pogody WMO.
    Każda mapa ma już nałożoną odpowiednią ikonę pogody.
    W trybie nocnym (mode="night") używa wariantów z księżycem.
    """
    if code == 0:
        return "map_moon.png" if mode == "night" else "map_sun.png"
    elif code in [1, 2]:
        return "map_cloud_moon.png" if mode == "night" else "map_cloud_sun.png"
    elif code == 3:
        return "map_cloud.png"
    elif code in [45, 48]:
        return "map_fog_moon.png" if mode == "night" else "map_fog.png"
    elif code in [51, 53, 61, 80]:
        return "map_rain_light.png"
    elif code in [55, 63, 65, 81, 82]:
        return "map_rain.png"
    elif code in [56, 57, 66, 67]:
        return "map_rain_snow.png"
    elif code in [71, 85]:
        return "map_snow_light.png"
    elif code in [73, 75, 77, 86]:
        return "map_snow.png"
    elif code in [95, 96, 99]:
        return "map_storm.png"
    else:
        return "map_cloud.png"

# ============================================
# LISTA DZIELNIC BOGUSZOWA-GORC
# ============================================

DISTRICTS = [
    {"name": "Lubominek",          "lat": 50.7750, "lon": 16.1900, "x": 385,  "y": 235},
    {"name": "Chełmiec",           "lat": 50.7789, "lon": 16.2110, "x": 669,  "y": 220},
    {"name": "Gorce",              "lat": 50.7600, "lon": 16.1950, "x": 154,  "y": 490},
    {"name": "Boguszów-Gorce",     "lat": 50.7551, "lon": 16.2049, "x": 594,  "y": 670},
    {"name": "Stary Lesieniec",    "lat": 50.7477, "lon": 16.1869, "x": 403,  "y": 830},
    {"name": "Kuźnice Świdnickie", "lat": 50.7469, "lon": 16.2204, "x": 750,  "y": 890},
    {"name": "Dzikowiec",          "lat": 50.7245, "lon": 16.2195, "x": 665,  "y": 1250},
]

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
    """Color scheme for temperature display on map"""
    if temp <= -10: return (100, 180, 255)   # Light blue (very cold)
    elif temp < 0:  return (50, 150, 255)    # Blue (cold)
    elif temp < 15: return (50, 180, 255)    # Blue (cool)
    elif temp < 20: return (50, 205, 50)     # Green (mild)
    elif temp < 25: return (255, 165, 0)     # Orange (warm)
    return (255, 100, 100)                   # Light red (hot)

# ============================================
# TEMPERATURE FORMATTING HELPER
# ============================================

def format_temp(temp: int) -> str:
    """Format temperature with sign, but no sign for zero"""
    if temp == 0:
        return "0°C"
    return f"{temp:+d}°C"

# ============================================
# WIND HELPERS
# ============================================

def get_wind_direction_name(degrees: float) -> str:
    """Convert wind direction in degrees to Polish cardinal direction name"""
    if degrees is None:
        return "zmienny"

    degrees = degrees % 360

    directions = [
        (22.5, "północny"),
        (67.5, "północno-wschodni"),
        (112.5, "wschodni"),
        (157.5, "południowo-wschodni"),
        (202.5, "południowy"),
        (247.5, "południowo-zachodni"),
        (292.5, "zachodni"),
        (337.5, "północno-zachodni"),
        (360.1, "północny"),
    ]

    for threshold, name in directions:
        if degrees < threshold:
            return name

    return "zmienny"

def get_wind_strength_description(speed_kmh: float) -> str:
    """Convert wind speed (km/h) to Polish description"""
    if speed_kmh is None or speed_kmh < 1:
        return "cisza"
    elif speed_kmh < 6:
        return "słaby"
    elif speed_kmh < 20:
        return "słaby"
    elif speed_kmh < 40:
        return "umiarkowany"
    elif speed_kmh < 60:
        return "dość silny"
    elif speed_kmh < 80:
        return "silny"
    else:
        return "bardzo silny"

# ============================================
# DAY/NIGHT DETECTION & FORECAST PERIOD
# ============================================

def get_forecast_mode():
    """
    Determine forecast mode based on current system time.

    Returns:
        tuple: (mode, current_hour, end_hour)
            mode: "day" or "night"
            current_hour: forecast start hour
            end_hour: forecast end hour (12 hours later)

    Logic:
        04:00-16:00 -> day mode (forecast until evening)
        16:00-04:00 -> night mode (forecast until morning)
    """
    now = datetime.now()
    current_hour = now.hour

    if 4 <= current_hour < 16:
        mode = "day"
        end_hour = min(current_hour + 12, 22)  # Don't forecast past 22:00
    else:
        mode = "night"
        # Night mode: forecast next 12 hours (may wrap to next day)
        end_hour = (current_hour + 12) % 24

    logger.info(f"🌓 Forecast mode: {mode.upper()} | Current: {current_hour}:00 | Horizon: next 12h")
    return mode, current_hour, end_hour


# ============================================
# METEOROLOGICAL ANALYSIS FUNCTIONS
# ============================================

def analyze_temperature_trend(temps, times):
    """
    Analyze temperature trend over forecast period.

    Returns:
        dict: {
            "trend": "rising" | "falling" | "stable",
            "change": float (total change in deg C),
            "rapid_changes": list of (time, change) tuples for front detection,
            "max_temp": float,
            "min_temp": float,
            "max_time": str,
            "min_time": str
        }
    """
    if not temps or len(temps) < 2:
        return {"trend": "stable", "change": 0, "rapid_changes": [], "max_temp": 0, "min_temp": 0}

    total_change = temps[-1] - temps[0]
    max_temp = max(temps)
    min_temp = min(temps)
    max_idx = temps.index(max_temp)
    min_idx = temps.index(min_temp)

    # Detect rapid changes (>3 deg C in 3 hours) - potential fronts
    rapid_changes = []
    for i in range(len(temps) - 3):
        change_3h = temps[i + 3] - temps[i]
        if abs(change_3h) >= 3:
            rapid_changes.append((times[i][11:16] if i < len(times) else "", change_3h))

    # Determine overall trend
    if total_change > 2:
        trend = "rising"
    elif total_change < -2:
        trend = "falling"
    else:
        trend = "stable"

    return {
        "trend": trend,
        "change": round(total_change, 1),
        "rapid_changes": rapid_changes,
        "max_temp": round(max_temp),
        "min_temp": round(min_temp),
        "max_time": times[max_idx][11:16] if max_idx < len(times) else "",
        "min_time": times[min_idx][11:16] if min_idx < len(times) else ""
    }


def detect_hazards(temps, precip_probs, weather_codes, wind_speeds):
    """
    Detect meteorological hazards.

    Returns:
        dict: {
            "freezing_rain_risk": bool,
            "snow_risk": bool,
            "fog_risk": bool,
            "strong_wind_risk": bool,
            "max_wind": float,
            "details": dict with specific hazard info
        }
    """
    hazards = {
        "freezing_rain_risk": False,
        "snow_risk": False,
        "fog_risk": False,
        "strong_wind_risk": False,
        "max_wind": max(wind_speeds) if wind_speeds else 0,
        "details": {}
    }

    # Freezing rain: temp -2 deg C to +2 deg C AND precip > 30%
    for i, temp in enumerate(temps):
        if -2 <= temp <= 2 and i < len(precip_probs) and precip_probs[i] > 30:
            hazards["freezing_rain_risk"] = True
            hazards["details"]["freezing_rain"] = f"temp {round(temp)}°C, opady {precip_probs[i]}%"
            break

    # Snow: temp < 1 deg C AND precip > 40%
    for i, temp in enumerate(temps):
        if temp < 1 and i < len(precip_probs) and precip_probs[i] > 40:
            hazards["snow_risk"] = True
            hazards["details"]["snow"] = f"temp {round(temp)}°C, opady {precip_probs[i]}%"
            break

    # Fog: WMO codes 45, 48
    for code in weather_codes:
        if code in [45, 48]:
            hazards["fog_risk"] = True
            hazards["details"]["fog"] = "kod pogody: mgła"
            break

    # Strong wind: avg > 50 km/h or gusts implied
    if hazards["max_wind"] > 50:
        hazards["strong_wind_risk"] = True
        hazards["details"]["wind"] = f"{round(hazards['max_wind'])} km/h"

    return hazards


# ============================================
# PROFESSIONAL NARRATIVE GENERATION
# ============================================

def generate_professional_forecast_text(hourly_data, mode):
    """
    Generate professional meteorological narrative forecast in Polish.

    Args:
        hourly_data: Dict with arrays: times, temps, precip_probs, weather_codes, wind_speeds, wind_dirs
        mode: "day" or "night"

    Returns:
        str: Professional Polish forecast text with emojis and meteorological storytelling
    """
    if not hourly_data or not hourly_data.get('temps'):
        logger.warning("⚠️ No hourly data for professional forecast, using fallback")
        return "Sprawdź temperaturę w swojej dzielnicy na mapie."

    times = hourly_data['times']
    temps = hourly_data['temps']
    precip_probs = hourly_data['precip_probs']
    weather_codes = hourly_data['weather_codes']
    wind_speeds = hourly_data['wind_speeds']
    wind_dirs = hourly_data['wind_dirs']

    # Analyze data
    trend = analyze_temperature_trend(temps, times)
    hazards = detect_hazards(temps, precip_probs, weather_codes, wind_speeds)

    # Build narrative
    parts = []

    # === OPENING: Temperature trend ===
    if mode == "day":
        intro = f"Prognoza na dzień:\n"
    else:
        intro = f"Prognoza na noc:\n"
    parts.append(intro)

    # Temperature narrative
    if trend['trend'] == "rising":
        if trend['change'] > 5:
            temp_story = f"📍 Temperatura będzie stopniowo rosnąć z {format_temp(trend['min_temp'])} " \
                        f"(ok. {trend['min_time']}) do {format_temp(trend['max_temp'])} " \
                        f"(ok. {trend['max_time']})."
        else:
            temp_story = f"📍 Temperatura utrzyma się z tendencją wzrostową, " \
                        f"osiągając maksymalnie {format_temp(trend['max_temp'])}."

    elif trend['trend'] == "falling":
        if trend['change'] < -5:
            temp_story = f"📍 Temperatura będzie stopniowo spadać z {format_temp(trend['max_temp'])} " \
                        f"do {format_temp(trend['min_temp'])} pod koniec okresu prognozy."
        else:
            temp_story = f"📍 Temperatura będzie powoli spadać, " \
                        f"osiągając minimum {format_temp(trend['min_temp'])}."

    else:  # stable
        avg_temp = round(sum(temps) / len(temps))
        temp_story = f"📍 Temperatura utrzyma się na stałym poziomie około {format_temp(avg_temp)}."

    parts.append(temp_story)

    # === RAPID CHANGES (Fronts) ===
    if trend['rapid_changes']:
        for time_str, change in trend['rapid_changes'][:1]:  # Only first front
            if change > 0:
                front_story = f"\nOkoło godz. {time_str} możliwy gwałtowny skok temperatury " \
                             f"(+{abs(round(change))}°C) - przejście frontu ciepłego lub adwekcja ciepła."
            else:
                front_story = f"\nOkoło godz. {time_str} możliwy gwałtowny spadek temperatury " \
                             f"({round(change)}°C) - przejście frontu zimnego."
            parts.append(front_story)

    # === SKY CONDITIONS & PRECIPITATION ===
    avg_code = round(sum(weather_codes) / len(weather_codes)) if weather_codes else 3
    max_precip = max(precip_probs) if precip_probs else 0

    # Determine sky description
    if avg_code <= 1:
        sky_desc = "Bezchmurnie"
    elif avg_code <= 3:
        sky_desc = "Zachmurzenie umiarkowane"
    elif avg_code in [45, 48]:
        sky_desc = "Mgliście"
    elif 51 <= avg_code <= 67:
        sky_desc = "Pochmurno z opadami deszczu"
    elif 71 <= avg_code <= 86:
        sky_desc = "Pochmurno z opadami śniegu"
    elif avg_code >= 95:
        sky_desc = "Burzowo"
    else:
        sky_desc = "Pochmurno"

    # Precipitation narrative
    if max_precip > 70:
        if avg_code >= 71:
            precip_story = f"\n{sky_desc}. Opady śniegu bardzo prawdopodobne (do {max_precip}%)."
        elif avg_code >= 51:
            precip_story = f"\n{sky_desc}. Opady deszczu bardzo prawdopodobne (do {max_precip}%)."
        else:
            precip_story = f"\n{sky_desc}. Opady prawdopodobne (do {max_precip}%)."

    elif max_precip > 40:
        precip_story = f"\n{sky_desc}. Miejscami możliwe słabe opady (szansa {max_precip}%)."

    elif max_precip > 20:
        precip_story = f"\n{sky_desc}. Niewielkie szanse opadów."

    else:
        precip_story = f"\n{sky_desc}. Bez opadów."

    parts.append(precip_story)

    # === WIND ===
    if wind_speeds:
        avg_wind = round(sum(wind_speeds) / len(wind_speeds))
        max_wind = round(max(wind_speeds))
        avg_dir = round(sum(wind_dirs) / len(wind_dirs)) if wind_dirs else 0

        wind_dir_name = get_wind_direction_name(avg_dir)
        wind_strength = get_wind_strength_description(avg_wind)

        if max_wind > avg_wind + 15:
            wind_story = f"\nWiatr {wind_dir_name} {wind_strength}, " \
                        f"średnio {avg_wind} km/h, w porywach do {max_wind} km/h."
        elif avg_wind >= 20:
            wind_story = f"\nWiatr {wind_dir_name} {wind_strength}, około {avg_wind} km/h."
        elif avg_wind >= 10:
            wind_story = f"\nWiatr {wind_dir_name} słaby, około {avg_wind} km/h."
        else:
            wind_story = "\nWiatr słaby lub cisza."

        parts.append(wind_story)

    # === HAZARD WARNINGS ===
    warnings = []

    if hazards['freezing_rain_risk']:
        warnings.append("UWAGA: Ryzyko marznącego deszczu - temperatura bliska 0°C przy opadach!")

    if hazards['snow_risk']:
        warnings.append("UWAGA: Możliwe opady śniegu z akumulacją!")

    if hazards['fog_risk']:
        warnings.append("UWAGA: Gęsta mgła - ograniczona widoczność!")

    if hazards['strong_wind_risk']:
        warnings.append(f"UWAGA: Silny wiatr do {round(hazards['max_wind'])} km/h!")

    if warnings:
        parts.append("\n\n" + "\n".join(warnings))

    # === CLOSING ===
    parts.append("\nSzczegóły dla poszczególnych dzielnic na mapie poniżej.")

    return "".join(parts)


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
    """
    Fetch hourly forecast for Boguszów-Gorce center district.
    Returns both legacy format and new hourly arrays for professional forecast.
    """

    # Boguszów-Gorce center coordinates
    lat = 50.7551
    lon = 16.2049

    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": [
            "temperature_2m",
            "precipitation_probability",
            "weather_code",
            "wind_speed_10m",
            "wind_direction_10m",
            "wind_gusts_10m"
        ],
        "timezone": "Europe/Warsaw",
        "forecast_days": 2
    }

    logger.info("Fetching forecast for Boguszów-Gorce (center)...")
    data = fetch_with_retry(OPENMETEO_URL, params, max_retries=2)

    if not data:
        logger.warning("Could not fetch forecast")
        return None

    hourly = data.get('hourly', {})
    times = hourly.get('time', [])
    temps = hourly.get('temperature_2m', [])
    precip_probs = hourly.get('precipitation_probability', [])
    weather_codes = hourly.get('weather_code', [])
    wind_speeds = hourly.get('wind_speed_10m', [])
    wind_directions = hourly.get('wind_direction_10m', [])
    wind_gusts = hourly.get('wind_gusts_10m', [])

    if not times or not temps:
        logger.warning("No hourly data in response")
        return None

    now = datetime.now()
    mode, current_hour, end_hour = get_forecast_mode()

    # Extract next 12 hours for professional forecast
    hourly_forecast = {
        'times': [],
        'temps': [],
        'precip_probs': [],
        'weather_codes': [],
        'wind_speeds': [],
        'wind_dirs': [],
        'wind_gusts': []
    }

    # Legacy day/night aggregation (keep for backward compatibility)
    day_temps = []
    night_temps = []
    day_precip = []
    night_precip = []
    day_codes = []
    night_codes = []
    day_wind_speeds = []
    day_wind_directions = []

    for i, time_str in enumerate(times):
        hour = int(time_str[11:13])
        day = int(time_str[8:10])
        dt = datetime.fromisoformat(time_str.replace('Z', '+00:00'))

        # Collect next 12 hours for professional forecast
        if len(hourly_forecast['times']) < 12 and dt >= now:
            hourly_forecast['times'].append(time_str)
            hourly_forecast['temps'].append(temps[i])
            hourly_forecast['precip_probs'].append(precip_probs[i] if i < len(precip_probs) else 0)
            hourly_forecast['weather_codes'].append(weather_codes[i] if i < len(weather_codes) else 0)
            hourly_forecast['wind_speeds'].append(wind_speeds[i] if i < len(wind_speeds) else 0)
            hourly_forecast['wind_dirs'].append(wind_directions[i] if i < len(wind_directions) else 0)
            hourly_forecast['wind_gusts'].append(wind_gusts[i] if i < len(wind_gusts) else 0)

        # Legacy aggregation
        if day == now.day and 6 <= hour < 18:
            day_temps.append(temps[i])
            day_precip.append(precip_probs[i] if i < len(precip_probs) else 0)
            day_codes.append(weather_codes[i] if i < len(weather_codes) else 0)
            if i < len(wind_speeds):
                day_wind_speeds.append(wind_speeds[i])
            if i < len(wind_directions):
                day_wind_directions.append(wind_directions[i])

        elif (day == now.day and hour >= 18) or (day == now.day + 1 and hour < 6):
            night_temps.append(temps[i])
            night_precip.append(precip_probs[i] if i < len(precip_probs) else 0)
            night_codes.append(weather_codes[i] if i < len(weather_codes) else 0)

    avg_wind_speed = round(sum(day_wind_speeds) / len(day_wind_speeds)) if day_wind_speeds else None
    avg_wind_direction = round(sum(day_wind_directions) / len(day_wind_directions)) if day_wind_directions else None
    max_wind_speed = round(max(day_wind_speeds)) if day_wind_speeds else None

    result = {
        # Legacy format
        "day_max": round(max(day_temps)) if day_temps else None,
        "day_min": round(min(day_temps)) if day_temps else None,
        "night_min": round(min(night_temps)) if night_temps else None,
        "day_precip_max": max(day_precip) if day_precip else 0,
        "night_precip_max": max(night_precip) if night_precip else 0,
        "day_codes": day_codes,
        "night_codes": night_codes,
        "wind_speed_avg": avg_wind_speed,
        "wind_speed_max": max_wind_speed,
        "wind_direction": avg_wind_direction,

        # New hourly data for professional forecast
        "hourly": hourly_forecast,
        "forecast_mode": mode
    }

    logger.info(f"✅ Forecast ({mode}): {len(hourly_forecast['temps'])} hourly points collected")
    return result

def generate_forecast_text(forecast: dict) -> str:
    """Generate forecast text with temperature, conditions and wind info (legacy fallback)"""

    if not forecast:
        return "Sprawdź temperaturę w swojej dzielnicy na mapie."

    sentences = []

    day_max = forecast.get('day_max')
    night_min = forecast.get('night_min')

    if day_max is not None:
        temp_text = f"Dziś maksymalnie {format_temp(day_max)}"
        if night_min is not None:
            temp_text += f", w nocy spadek do {format_temp(night_min)}."
        else:
            temp_text += "."
        sentences.append(temp_text)
    elif night_min is not None:
        sentences.append(f"W nocy temperatura spadnie do {format_temp(night_min)}.")

    day_precip = forecast.get('day_precip_max', 0)
    night_precip = forecast.get('night_precip_max', 0)
    day_codes = forecast.get('day_codes', [])

    avg_code = sum(day_codes) / len(day_codes) if day_codes else 3

    if avg_code <= 1:
        sky = "Bezchmurnie"
    elif avg_code <= 3:
        sky = "Zachmurzenie umiarkowane"
    elif avg_code <= 48:
        sky = "Mgliście"
    elif avg_code <= 67:
        sky = "Zachmurzenie z opadami deszczu"
    elif avg_code <= 86:
        sky = "Zachmurzenie z opadami śniegu"
    else:
        sky = "Pochmurno"

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

    wind_speed = forecast.get('wind_speed_avg')
    wind_max = forecast.get('wind_speed_max')
    wind_dir = forecast.get('wind_direction')

    if wind_speed is not None and wind_speed >= 1:
        wind_strength = get_wind_strength_description(wind_speed)
        wind_direction = get_wind_direction_name(wind_dir)

        if wind_max and wind_max > wind_speed + 10:
            wind_text = f"Wiatr {wind_direction} {wind_strength}, {wind_speed}-{wind_max} km/h."
        else:
            wind_text = f"Wiatr {wind_direction} {wind_strength}, ok. {wind_speed} km/h."

        sentences.append(wind_text)
    else:
        sentences.append("Wiatr słaby lub cisza.")

    return " ".join(sentences)

# ============================================
# MAP IMAGE GENERATION
# ============================================

def add_charity_overlay(img):
    """
    Add charity overlay image to the weather map.
    Applies drop shadow if enabled.
    """
    if not OVERLAY_ENABLED:
        return img

    if not OVERLAY_IMAGE.exists():
        logger.warning(f"⚠️ Overlay image not found: {OVERLAY_IMAGE}")
        return img

    try:
        overlay = Image.open(OVERLAY_IMAGE).convert('RGBA')
        x, y = OVERLAY_POSITION

        if OVERLAY_SHADOW_ENABLED and OVERLAY_SHADOW_OFFSET > 0:
            # Expanded canvas shadow (same technique as weather icons)
            margin = OVERLAY_SHADOW_BLUR * 3
            ow, oh = overlay.size

            # Build shadow from overlay's alpha channel
            solid = Image.new('RGBA', (ow, oh), OVERLAY_SHADOW_COLOR)
            alpha = overlay.split()[3]
            solid.putalpha(alpha)

            # Place into expanded canvas so blur spreads naturally
            shadow = Image.new('RGBA', (ow + 2 * margin, oh + 2 * margin), (0, 0, 0, 0))
            shadow.paste(solid, (margin, margin), solid)
            shadow = shadow.filter(ImageFilter.GaussianBlur(radius=OVERLAY_SHADOW_BLUR))

            # Paste shadow first (offset by shadow amount, adjusted for margin)
            shadow_pos = (x - margin + OVERLAY_SHADOW_OFFSET, y - margin + OVERLAY_SHADOW_OFFSET)
            img.paste(shadow, shadow_pos, shadow)

        # Paste the overlay image
        img.paste(overlay, (x, y), overlay)
        logger.info(f"✅ Charity overlay added at position {OVERLAY_POSITION}")

    except Exception as e:
        logger.error(f"❌ Error adding overlay: {e}")

    return img

def draw_text_centered(draw, x, y, text, font, color, stroke_width=3, stroke_fill=(0,0,0)):
    bbox = draw.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]

    pos_x = x - text_w // 2
    pos_y = y - text_h // 2

    draw.text((pos_x, pos_y), text, font=font, fill=color,
              stroke_width=stroke_width, stroke_fill=stroke_fill)

def generate_map_image(districts_data: list, weather_code: int, mode: str = "day") -> tuple:
    """
    Generuje mapę z temperaturami.
    Wybiera odpowiednią mapę bazową na podstawie kodu pogody i trybu (dzień/noc).
    """
    map_filename = get_map_for_code(weather_code, mode)
    input_path = MAPS_DIR / map_filename
    output_path = PROJECT_ROOT / "output" / OUTPUT_IMAGE_FILENAME

    # Ensure output directory exists
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if not input_path.exists():
        logger.error(f"❌ Brak pliku mapy: {input_path}")
        input_path = MAPS_DIR / "map_cloud.png"
        if not input_path.exists():
            logger.error(f"❌ Brak również mapy fallback: {input_path}")
            return None, 0, 0
        logger.warning(f"⚠️ Używam mapy fallback: map_cloud.png")

    logger.info(f"📍 Używam mapy: {map_filename} (kod pogody: {weather_code})")

    try:
        img = Image.open(input_path).convert('RGBA')
        draw = ImageDraw.Draw(img)

        font_temp = get_font(55, bold=True)
        font_info = get_font(24, bold=True)

        min_temp = 100
        max_temp = -100

        for d in districts_data:
            temp = round(d['temp'])
            if temp < min_temp: min_temp = temp
            if temp > max_temp: max_temp = temp

            temp_str = f"{temp:+d}°" if temp != 0 else "0°"
            color = get_temp_color(temp)

            draw_text_centered(draw, d['x'], d['y'], temp_str, font_temp, color)

        now_str = datetime.now().strftime("%d.%m.%Y godz. %H:%M")
        footer_text = f"Stan na: {now_str} | Dane: Open-Meteo"

        w, h = img.size
        draw.text((w - 450, h - 25), footer_text, font=font_info, fill=(0, 0, 0))

        # Add charity overlay before saving
        img = add_charity_overlay(img)

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
    """Setup Chrome driver using Docker Selenium (always Docker for Boguszów-Gorce)."""
    from docker_selenium import get_docker_driver
    logger.info("🐳 Using Docker Selenium...")
    return get_docker_driver(max_retries=3)


def setup_chrome_driver_with_retry():
    """Setup Chrome driver with automatic recovery and retry on failure."""
    try:
        return setup_chrome_driver()
    except Exception as e:
        logger.error(f"❌ Docker Selenium failed: {e}")
        logger.error("=" * 60)
        logger.error("DOCKER TROUBLESHOOTING:")
        logger.error("  1. Check container: docker ps")
        logger.error("  2. View logs: docker logs bg-selenium-chrome")
        logger.error("  3. Restart: docker compose -f docker-compose.yml restart")
        logger.error("  4. Re-login: python src/docker_fb_login.py")
        logger.error("=" * 60)
        raise


def handle_cookie_consent(driver) -> bool:
    """
    Handle Facebook cookie consent popup.
    Returns True if popup was handled, False if no popup found.
    """
    logger.info("🍪 Checking for cookie consent popup...")

    # Take screenshot before attempting to handle cookie popup
    driver.save_screenshot(str(PROJECT_ROOT / "debug" / "debug_before_cookie_check.png"))

    # Multiple selector strategies
    cookie_selectors = [
        # Strategy 1: Exact button text with normalize-space
        "//button[normalize-space()='Allow all cookies']",
        "//button[normalize-space()='Decline optional cookies']",

        # Strategy 2: Button containing text
        "//button[contains(., 'Allow all cookies')]",
        "//button[contains(., 'Decline optional')]",

        # Strategy 3: Span inside button
        "//button//span[contains(text(), 'Allow all')]/..",
        "//button//span[contains(text(), 'Decline optional')]/..",

        # Strategy 4: Role-based within dialog
        "//div[@role='dialog']//button[contains(., 'Allow')]",
        "//div[@role='dialog']//button[contains(., 'Decline')]",

        # Strategy 5: Polish versions
        "//button[contains(., 'Zezwól na wszystkie')]",
        "//button[contains(., 'Akceptuj wszystkie')]",
        "//button[contains(., 'Odrzuć opcjonalne')]",
        "//button[normalize-space()='Zezwól na wszystkie pliki cookie']",

        # Strategy 6: aria-label based
        "//button[@aria-label='Allow all cookies']",
        "//button[@aria-label='Decline optional cookies']",

        # Strategy 7: data-testid (if available)
        "//button[@data-testid='cookie-policy-manage-dialog-accept-button']",
    ]

    for sel in cookie_selectors:
        try:
            cookie_btn = WebDriverWait(driver, 2).until(
                EC.presence_of_element_located((By.XPATH, sel))
            )
            if cookie_btn:
                logger.info(f"🍪 Found cookie button: {sel}")
                # Scroll into view
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", cookie_btn)
                human_delay(0.3, 0.5)
                # Try JS click (more reliable than regular click)
                driver.execute_script("arguments[0].click();", cookie_btn)
                logger.info(f"✅ Clicked cookie button via JS: {sel}")
                human_delay(3, 4)
                return True
        except Exception as e:
            logger.debug(f"Selector failed: {sel} - {e}")
            continue

    # Fallback: Try to find any button in cookie dialog by structure
    try:
        # Look for the specific dialog structure from screenshot
        dialog_buttons = driver.find_elements(By.XPATH,
            "//div[contains(@class, 'x1n2onr6')]//button | //div[@role='dialog']//button"
        )
        logger.info(f"🔍 Found {len(dialog_buttons)} buttons in dialog area")

        for i, btn in enumerate(dialog_buttons):
            try:
                btn_text = btn.text.strip()
                logger.info(f"  Button #{i+1}: '{btn_text}'")

                # Click "Allow all cookies" or similar
                if 'allow' in btn_text.lower() or 'zezwól' in btn_text.lower() or 'akceptuj' in btn_text.lower():
                    driver.execute_script("arguments[0].click();", btn)
                    logger.info(f"✅ Clicked button by text scan: '{btn_text}'")
                    human_delay(3, 4)
                    return True
            except:
                continue
    except Exception as e:
        logger.debug(f"Fallback button scan failed: {e}")

    logger.info("ℹ️ No cookie popup found (or already accepted)")
    return False


def ensure_logged_in_as_page(driver):
    """Navigate to FB page and ensure we're logged in as the page.

    Uses 3-stage approach:
    - STAGE A: Check for immediate "Przełącz profil" modal popup
    - STAGE B: Look for sidebar "Przełącz teraz" button
    - STAGE C: Fallback - use top-right profile menu to switch
    """

    target_profile_name = FB_PAGE_NAME

    logger.info("📍 Opening FB page to verify login...")
    driver.get(FB_PAGE_URL)
    human_delay(4, 6)

    # =========================================================
    # HANDLE COOKIE CONSENT POPUP FIRST (before login check!)
    # This popup appears after cold boot and blocks everything
    # =========================================================
    handle_cookie_consent(driver)

    # =========================================================
    # NOW check if login needed (after cookie popup is gone)
    # =========================================================
    login_elements = driver.find_elements(By.NAME, "email")
    login_visible = any(el.is_displayed() for el in login_elements) if login_elements else False

    if login_visible:
        logger.warning("⚠️ LOGIN DETECTED! Pausing 120s for manual login...")
        driver.save_screenshot(str(PROJECT_ROOT / "debug" / "debug_login_detected.png"))
        time.sleep(120)
        driver.get(FB_PAGE_URL)
        human_delay(4, 6)
        # Handle cookie popup again after login
        handle_cookie_consent(driver)

    logger.info(f"🔄 Ensuring we are switched to: {target_profile_name}")

    switched = False

    # ---------------------------------------------------------
    # STAGE A: Check for "Przełącz profil" MODAL (Pop-up)
    # ---------------------------------------------------------
    try:
        modal_switch_btn = WebDriverWait(driver, 4).until(
            EC.element_to_be_clickable((By.XPATH, "//div[@role='dialog']//span[text()='Przełącz']/ancestor::div[@role='button']"))
        )
        if modal_switch_btn:
            logger.info("✅ STAGE A: Found 'Przełącz' modal popup immediately.")
            modal_switch_btn.click()
            switched = True
            human_delay(3, 5)
    except:
        logger.info("ℹ️ STAGE A: No immediate modal popup found.")

    # ---------------------------------------------------------
    # STAGE B: Check for Standard Sidebar "Przełącz teraz" Button
    # ---------------------------------------------------------
    if not switched:
        logger.info("🔄 STAGE B: Looking for sidebar 'Przełącz teraz' button...")
        switch_now_selectors = [
            "//span[text()='Przełącz teraz']",
            "//div[@role='button']//span[text()='Przełącz teraz']",
            "//div[contains(@class, 'x1i10hfl')]//span[text()='Przełącz teraz']",
        ]

        for selector in switch_now_selectors:
            try:
                btn = WebDriverWait(driver, 3).until(
                    EC.element_to_be_clickable((By.XPATH, selector))
                )
                if btn:
                    logger.info(f"✅ STAGE B: Found sidebar button: {selector}")
                    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", btn)
                    human_delay(0.5, 1)
                    btn.click()

                    # Handle the confirmation dialog
                    human_delay(1, 2)
                    confirm_selectors = [
                        "//div[@role='dialog']//span[text()='Przełącz']",
                        "//div[@role='dialog']//div[@role='button']//span[text()='Przełącz']",
                    ]
                    for c_sel in confirm_selectors:
                        try:
                            c_btn = WebDriverWait(driver, 3).until(EC.element_to_be_clickable((By.XPATH, c_sel)))
                            c_btn.click()
                            logger.info("✅ STAGE B: Confirmed switch in dialog")
                            break
                        except:
                            pass

                    switched = True
                    human_delay(3, 5)
                    break
            except:
                continue

    # ---------------------------------------------------------
    # STAGE C: UNIVERSAL FALLBACK - Top-Right Menu
    # ---------------------------------------------------------
    if not switched:
        logger.info("⚠️ STAGE B failed. Executing STAGE C: Top-Right Menu Switch strategy.")

        menu_opened = False

        # Selectors for the profile menu button in top-right corner
        account_menu_selectors = [
            "//div[@role='button'][@aria-label='Twój profil']",
            "//div[@aria-label='Twój profil']",
            "//svg[@aria-label='Twój profil']/ancestor::div[@role='button']",
            "//div[@aria-label='Mechanizmy kontrolne i ustawienia konta']//div[@role='button']",
            "//div[@aria-label='Your profile']",
            "//div[@aria-label='Account controls and settings']//div[@role='button']",
            "//div[@role='navigation']//div[@role='button']//image",
            "//div[@role='banner']//div[@role='button'][.//image]",
        ]

        # Attempt 1: Standard Selectors with JavaScript Click
        for sel in account_menu_selectors:
            try:
                menu_btn = WebDriverWait(driver, 3).until(EC.presence_of_element_located((By.XPATH, sel)))
                driver.execute_script("arguments[0].style.border='3px solid red'", menu_btn)
                logger.info(f"✅ STAGE C: Found menu button: {sel}")
                driver.execute_script("arguments[0].click();", menu_btn)
                menu_opened = True
                human_delay(2, 3)
                break
            except:
                continue

        # Attempt 2: Coordinate Click (Force) if selectors fail
        if not menu_opened:
            logger.warning("⚠️ STAGE C: Selectors failed. Clicking Top-Right coordinates...")
            try:
                action = ActionChains(driver)
                action.move_by_offset(1860, 45).click().perform()
                action.move_by_offset(-1860, -45).perform()
                logger.info("✅ STAGE C: Clicked coordinates (1860, 45)")
                menu_opened = True
                human_delay(2, 3)
            except Exception as e:
                logger.error(f"❌ STAGE C: Coordinate click failed: {e}")

        # If menu is open, find the target profile
        if menu_opened:
            try:
                target_xpath = f"//span[contains(text(), '{target_profile_name}')]"

                target_profile = WebDriverWait(driver, 5).until(
                    EC.element_to_be_clickable((By.XPATH, target_xpath))
                )
                target_profile.click()
                logger.info(f"✅ STAGE C: Clicked target profile '{target_profile_name}'")
                switched = True
                human_delay(5, 7)

            except:
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
                        except:
                            continue

                    # Now try finding the name again
                    target_profile = WebDriverWait(driver, 5).until(
                        EC.element_to_be_clickable((By.XPATH, target_xpath))
                    )
                    target_profile.click()
                    logger.info(f"✅ STAGE C: Clicked target profile after expanding list")
                    switched = True
                    human_delay(5, 7)
                except Exception as e:
                    logger.error(f"❌ STAGE C failed to find profile in menu: {e}")
                    driver.save_screenshot(str(PROJECT_ROOT / "debug" / "debug_stage_c_fail.png"))
        else:
            logger.error("❌ STAGE C: Could not open menu.")

    if not switched:
        logger.warning("⚠️ Could not verify profile switch. Attempting to proceed (maybe already correct?)...")
    else:
        logger.info("✅ Profile switch logic completed.")
        human_delay(3, 4)

    return True


def post_to_facebook_selenium(driver, image_path: str, caption: str, test_mode: bool = False) -> bool:
    """Post image with caption to Facebook using Selenium

    Args:
        driver: Selenium WebDriver instance
        image_path: Path to image file
        caption: Post caption text
        test_mode: If True, prepare post but don't publish (screenshot instead)

    Returns:
        True if successful (or ready to publish in test_mode), False otherwise
    """

    try:
        # ============================================
        # STEP 1: FIND AND CLICK "CO SŁYCHAĆ?" INPUT
        # ============================================

        logger.info("🔍 Looking for post creation area...")

        driver.execute_script("window.scrollTo(0, 0);")
        human_delay(1, 2)

        post_box_found = False

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
                    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", post_box)
                    human_delay(0.5, 1)

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
            driver.save_screenshot(str(PROJECT_ROOT / "debug" / "debug_no_post_box.png"))
            return False

        # ============================================
        # STEP 2: WAIT FOR POST DIALOG MODAL
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
        # STEP 3: UPLOAD IMAGE (directly via hidden input)
        # ============================================

        logger.info(f"📤 Uploading image: {image_path}")

        file_inputs = driver.find_elements(By.XPATH, "//input[@type='file']")
        logger.info(f"Found {len(file_inputs)} file input(s)")

        for i, fi in enumerate(file_inputs):
            accept = fi.get_attribute('accept') or 'none'
            multiple = fi.get_attribute('multiple') or 'false'
            logger.info(f"  Input #{i+1}: accept='{accept[:50]}...', multiple={multiple}")

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
            driver.save_screenshot(str(PROJECT_ROOT / "debug" / "debug_no_upload.png"))
            return False

        logger.info("⏳ Waiting for image to process...")
        human_delay(6, 8)

        driver.save_screenshot(str(PROJECT_ROOT / "debug" / "debug_after_upload.png"))

        try:
            img_preview = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.XPATH, "//div[@role='dialog']//img[contains(@src, 'blob:')]"))
            )
            logger.info("✅ Uploaded image (blob:) visible in dialog")
        except:
            try:
                img_preview = driver.find_element(By.XPATH, "//div[@role='dialog']//img[contains(@class, 'x1ey2m1c')]")
                logger.info("✅ Image visible in dialog (FB class)")
            except:
                logger.warning("⚠️ Could not verify image in dialog - check debug_after_upload.png")

        # ============================================
        # STEP 4: ENTER CAPTION TEXT
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

            lines = caption.split('\n')
            for i, line in enumerate(lines):
                if line:
                    actions = ActionChains(driver)
                    actions.send_keys(line)
                    actions.perform()
                    human_delay(0.2, 0.3)

                if i < len(lines) - 1:
                    actions = ActionChains(driver)
                    actions.key_down(Keys.SHIFT).send_keys(Keys.ENTER).key_up(Keys.SHIFT)
                    actions.perform()
                    human_delay(0.1, 0.2)

            logger.info("✅ Caption entered line by line")

            human_delay(1, 2)
        else:
            logger.warning("⚠️ Could not find text area")
            driver.save_screenshot(str(PROJECT_ROOT / "debug" / "debug_no_textarea.png"))

        # ============================================
        # STEP 5: CLICK PUBLISH BUTTON (2-step: Dalej -> Opublikuj)
        # ============================================

        logger.info("🚀 Looking for publish button...")
        human_delay(2, 3)

        driver.save_screenshot(str(PROJECT_ROOT / "debug" / "debug_before_publish.png"))

        publish_selectors = [
            (By.XPATH, "//div[@role='dialog']//span[text()='Dalej']"),
            (By.XPATH, "//span[text()='Dalej']"),
            (By.XPATH, "//div[@role='dialog']//span[text()='Next']"),
            (By.XPATH, "//div[@role='dialog']//span[text()='Opublikuj']"),
            (By.XPATH, "//div[@role='dialog']//div[@aria-label='Opublikuj']"),
            (By.XPATH, "//span[text()='Opublikuj']/ancestor::div[@role='button']"),
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

            human_delay(3, 4)

            # Look for final "Opublikuj" button if we clicked "Dalej"
            final_publish_selectors = [
                (By.XPATH, "//div[@role='dialog']//span[text()='Opublikuj']"),
                (By.XPATH, "//div[@role='dialog']//div[@aria-label='Opublikuj']"),
                (By.XPATH, "//span[text()='Opublikuj']"),
                (By.XPATH, "//div[@role='dialog']//span[text()='Post']"),
            ]

            final_btn = None
            for by, selector in final_publish_selectors:
                try:
                    final_btn = WebDriverWait(driver, 5).until(
                        EC.element_to_be_clickable((by, selector))
                    )
                    if final_btn:
                        logger.info(f"✅ Found final publish button: {selector}")
                        break
                except:
                    continue

            # ============================================
            # TEST MODE: Stop here, take screenshot, don't publish
            # ============================================
            if test_mode:
                logger.info("=" * 60)
                logger.info("🧪 TEST MODE: Post is ready to publish!")
                logger.info("🧪 Taking screenshot and stopping before actual publish...")
                driver.save_screenshot(str(PROJECT_ROOT / "debug" / "debug_test_mode_ready.png"))
                logger.info(f"📸 Screenshot saved: {PROJECT_ROOT / 'debug' / 'debug_test_mode_ready.png'}")
                logger.info("🧪 Pipeline test PASSED - all steps completed successfully!")
                logger.info("=" * 60)
                return True

            # ============================================
            # PRODUCTION MODE: Actually publish
            # ============================================
            if final_btn:
                human_delay(0.5, 1)
                final_btn.click()
                logger.info("✅ Clicked final Opublikuj!")

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

            human_delay(4, 6)

            try:
                WebDriverWait(driver, 10).until_not(
                    EC.presence_of_element_located((By.XPATH, "//div[@role='dialog'][.//span[text()='Utwórz post']]"))
                )
                logger.info("✅ Post dialog closed!")
            except:
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

            driver.save_screenshot(str(PROJECT_ROOT / "debug" / "debug_after_publish.png"))

            human_delay(2, 3)
            driver.refresh()
            human_delay(3, 4)

            try:
                post_indicator = driver.find_element(By.XPATH, "//div[contains(text(), 'Aktualna temperatura')]")
                logger.info("✅ Post verified on page!")
            except:
                logger.warning("⚠️ Could not verify post on page - check manually")

            logger.info("✅ Post published successfully!")
            return True
        else:
            logger.error("❌ Could not find publish button")
            driver.save_screenshot(str(PROJECT_ROOT / "debug" / "debug_no_publish.png"))
            return False

    except Exception as e:
        logger.error(f"❌ Selenium error: {e}")
        import traceback
        traceback.print_exc()
        driver.save_screenshot(str(PROJECT_ROOT / "debug" / "debug_error.png"))
        return False


# ============================================
# GROUP SHARING FUNCTIONS
# ============================================

def get_latest_post_url(driver) -> str:
    """Get the URL of the most recent post on the page feed.

    After posting, navigates back to the page and finds the newest post link.
    Falls back to page URL if no specific post URL is found.

    Returns:
        Post URL string, or FB_PAGE_URL as fallback.
    """
    try:
        logger.info("=" * 50)
        logger.info("🔍 LOOKING FOR LATEST POST URL")
        logger.info("=" * 50)
        logger.info(f"📍 Navigating to page: {FB_PAGE_URL}")

        driver.get(FB_PAGE_URL)
        human_delay(3, 5)

        driver.save_screenshot(str(PROJECT_ROOT / "debug" / "debug_group_share_01_page_loaded.png"))
        logger.info("📸 Screenshot saved: debug_group_share_01_page_loaded.png")

        # Log current URL to verify we're on the right page
        logger.info(f"📍 Current URL after navigation: {driver.current_url}")

        # Find post links - look for posts with timestamps that link to individual posts
        post_link_selectors = [
            "//a[contains(@href, '/posts/')]",
            "//a[contains(@href, 'story_fbid')]",
            "//a[contains(@href, '/permalink/')]",
        ]

        for selector in post_link_selectors:
            try:
                links = driver.find_elements(By.XPATH, selector)
                logger.info(f"🔍 Selector '{selector}' found {len(links)} links")
                for link in links[:5]:  # Check first 5 matches
                    href = link.get_attribute('href')
                    if href and ('posts' in href or 'story_fbid' in href or 'permalink' in href):
                        # Only accept posts from our own page (not from followed pages)
                        if 'kangurello' not in href and '100027689516729' not in href:
                            logger.debug(f"  Skipping foreign post: {href[:80]}")
                            continue
                        # Clean up the URL
                        if '?' in href:
                            href = href.split('?')[0]
                        logger.info(f"✅ Found post URL: {href}")
                        return href
            except Exception as e:
                logger.debug(f"Selector {selector} failed: {e}")
                continue

        logger.warning("⚠️ Could not find specific post URL, falling back to page URL")
        logger.warning(f"⚠️ Fallback URL: {FB_PAGE_URL}")
        return FB_PAGE_URL

    except Exception as e:
        logger.error(f"❌ Error getting post URL: {e}")
        import traceback
        traceback.print_exc()
        return FB_PAGE_URL


def switch_to_personal_profile(driver) -> bool:
    """Switch from page profile back to personal profile for group sharing.

    Group sharing must be done as a personal profile, not as a page.
    Uses the same 3-stage menu approach: top-right menu -> see all profiles -> click personal.

    Returns:
        True if switch succeeded (or was already on personal), False on failure.
    """
    try:
        logger.info("=" * 50)
        logger.info(f"🔄 SWITCHING TO PERSONAL PROFILE: {PERSONAL_PROFILE_NAME}")
        logger.info("=" * 50)

        driver.save_screenshot(str(PROJECT_ROOT / "debug" / "debug_group_share_02_before_profile_switch.png"))
        logger.info("📸 Screenshot saved: debug_group_share_02_before_profile_switch.png")

        # Step 1: Click on profile menu (top-right)
        menu_selectors = [
            "//div[@role='button'][@aria-label='Twój profil']",
            "//div[@role='button'][@aria-label='Your profile']",
            "//div[@aria-label='Konto' and @role='button']",
            "//div[@aria-label='Account' and @role='button']",
        ]

        menu_clicked = False
        for selector in menu_selectors:
            try:
                logger.info(f"🔍 [Menu] Trying selector: {selector}")
                menu_btn = WebDriverWait(driver, 5).until(
                    EC.element_to_be_clickable((By.XPATH, selector))
                )
                menu_btn.click()
                menu_clicked = True
                logger.info(f"✅ [Menu] Clicked profile menu: {selector}")
                human_delay(2, 3)
                break
            except Exception as e:
                logger.debug(f"[Menu] Selector failed: {selector} - {e}")
                continue

        if not menu_clicked:
            logger.error("❌ [Menu] Could not find profile menu button")
            logger.error("❌ [Menu] Available elements on page (role='button'):")
            try:
                buttons = driver.find_elements(By.XPATH, "//div[@role='button']")
                for btn in buttons[:10]:
                    aria = btn.get_attribute('aria-label') or '(no aria-label)'
                    text = btn.text[:50] if btn.text else '(no text)'
                    logger.error(f"    - aria-label='{aria}', text='{text}'")
            except Exception:
                pass
            driver.save_screenshot(str(PROJECT_ROOT / "debug" / "debug_group_share_error_no_menu.png"))
            logger.error("📸 Screenshot saved: debug_group_share_error_no_menu.png")
            return False

        driver.save_screenshot(str(PROJECT_ROOT / "debug" / "debug_group_share_03_menu_opened.png"))
        logger.info("📸 Screenshot saved: debug_group_share_03_menu_opened.png")

        # Step 2: Look for personal profile name or "See all profiles" in the menu
        profile_selectors = [
            f"//span[contains(text(), '{PERSONAL_PROFILE_NAME}')]",
            f"//div[contains(text(), '{PERSONAL_PROFILE_NAME}')]",
            "//span[contains(text(), 'Zobacz wszystkie profile')]",
            "//span[contains(text(), 'See all profiles')]",
        ]

        profile_found = False
        for selector in profile_selectors:
            try:
                logger.info(f"🔍 [Profile] Trying selector: {selector}")
                profile_btn = WebDriverWait(driver, 3).until(
                    EC.element_to_be_clickable((By.XPATH, selector))
                )
                profile_btn.click()
                logger.info(f"✅ [Profile] Clicked: {selector}")
                profile_found = True
                human_delay(2, 3)

                # If we clicked "See all profiles", now look for personal profile in the list
                if 'wszystkie' in selector.lower() or 'all profiles' in selector.lower():
                    logger.info("📋 [Profile] 'See all profiles' expanded, looking for personal profile...")
                    driver.save_screenshot(str(PROJECT_ROOT / "debug" / "debug_group_share_04_all_profiles.png"))
                    logger.info("📸 Screenshot saved: debug_group_share_04_all_profiles.png")
                    try:
                        personal = WebDriverWait(driver, 5).until(
                            EC.element_to_be_clickable((By.XPATH, f"//span[contains(text(), '{PERSONAL_PROFILE_NAME}')]"))
                        )
                        personal.click()
                        logger.info(f"✅ [Profile] Switched to personal profile: {PERSONAL_PROFILE_NAME}")
                        human_delay(2, 3)
                    except Exception as e:
                        logger.warning(f"⚠️ [Profile] Could not find personal profile in list: {e}")
                        # Log what profiles ARE visible
                        try:
                            spans = driver.find_elements(By.XPATH, "//div[@role='dialog']//span")
                            logger.warning(f"⚠️ [Profile] Visible spans in dialog ({len(spans)} total):")
                            for s in spans[:15]:
                                if s.text.strip():
                                    logger.warning(f"    - '{s.text.strip()}'")
                        except Exception:
                            pass
                break
            except Exception as e:
                logger.debug(f"[Profile] Selector failed: {selector} - {e}")
                continue

        if not profile_found:
            logger.warning("⚠️ [Profile] Could not find profile switch button, may already be on personal profile")
            driver.save_screenshot(str(PROJECT_ROOT / "debug" / "debug_group_share_error_no_profile.png"))
            logger.warning("📸 Screenshot saved: debug_group_share_error_no_profile.png")

        driver.save_screenshot(str(PROJECT_ROOT / "debug" / "debug_group_share_05_after_profile_switch.png"))
        logger.info("📸 Screenshot saved: debug_group_share_05_after_profile_switch.png")
        logger.info("✅ [Profile] Profile switch procedure completed")
        return True

    except Exception as e:
        logger.error(f"❌ [Profile] Error switching profile: {e}")
        import traceback
        traceback.print_exc()
        driver.save_screenshot(str(PROJECT_ROOT / "debug" / "debug_group_share_error_switch.png"))
        logger.error("📸 Screenshot saved: debug_group_share_error_switch.png")
        return False


def share_post_to_group(driver, post_url: str, group_search_name: str, caption: str) -> bool:
    """Share a post to a specific Facebook group.

    Flow: navigate to post -> click Share -> Share to group -> search group ->
    select group -> enter caption -> publish.

    Args:
        driver: Selenium WebDriver instance
        post_url: URL of the post to share
        group_search_name: Search term to find the group in the share dialog
        caption: Text to add to the shared post

    Returns:
        True if sharing succeeded, False otherwise.
    """
    safe_group_name = "".join(c if c.isalnum() else "_" for c in group_search_name[:20])

    try:
        logger.info("=" * 50)
        logger.info(f"📤 SHARING TO GROUP: {group_search_name}")
        logger.info(f"📍 Post URL: {post_url}")
        logger.info(f"📛 Safe name for screenshots: {safe_group_name}")
        logger.info("=" * 50)

        # --- Step 1: Navigate to the post ---
        logger.info(f"🔗 [Step 1/6] Navigating to post...")
        driver.get(post_url)
        human_delay(3, 5)

        logger.info(f"📍 Current URL: {driver.current_url}")
        driver.save_screenshot(str(PROJECT_ROOT / "debug" / f"debug_share_{safe_group_name}_01_post_loaded.png"))
        logger.info(f"📸 Screenshot: debug_share_{safe_group_name}_01_post_loaded.png")

        # --- Step 2: Find and click Share button (Udostępnij) ---
        logger.info(f"🔍 [Step 2/6] Looking for Share button...")
        share_selectors = [
            "//span[text()='Udostępnij']",
            "//span[text()='Share']",
            "//div[@aria-label='Wyślij do innych']",
            "//div[@aria-label='Send this to friends or post it on your timeline']",
        ]

        share_clicked = False
        for selector in share_selectors:
            try:
                logger.info(f"  🔍 Trying: {selector}")
                share_btn = WebDriverWait(driver, 5).until(
                    EC.element_to_be_clickable((By.XPATH, selector))
                )
                share_btn.click()
                share_clicked = True
                logger.info(f"  ✅ Clicked share button: {selector}")
                human_delay(2, 3)
                break
            except Exception as e:
                logger.debug(f"  Share selector failed: {selector} - {e}")
                continue

        if not share_clicked:
            logger.error("❌ [Step 2/6] Could not find Share button on post")
            # Log what's visible for debugging
            try:
                spans = driver.find_elements(By.XPATH, "//span")
                share_like_spans = [s.text for s in spans if s.text and ('udostępnij' in s.text.lower() or 'share' in s.text.lower())]
                logger.error(f"❌ Spans containing 'share/udostępnij': {share_like_spans[:10]}")
            except Exception:
                pass
            driver.save_screenshot(str(PROJECT_ROOT / "debug" / f"debug_share_{safe_group_name}_error_no_share.png"))
            return False

        driver.save_screenshot(str(PROJECT_ROOT / "debug" / f"debug_share_{safe_group_name}_02_share_menu.png"))
        logger.info(f"📸 Screenshot: debug_share_{safe_group_name}_02_share_menu.png")

        # --- Step 3: Click "Udostępnij w grupie" / "Share to a group" ---
        logger.info(f"🔍 [Step 3/6] Looking for 'Share to group' option...")
        group_share_selectors = [
            "//span[text()='Udostępnij w grupie']",
            "//span[text()='Share to a group']",
            "//span[contains(text(), 'grupie')]",
            "//span[contains(text(), 'group')]",
        ]

        group_option_clicked = False
        for selector in group_share_selectors:
            try:
                logger.info(f"  🔍 Trying: {selector}")
                group_btn = WebDriverWait(driver, 5).until(
                    EC.element_to_be_clickable((By.XPATH, selector))
                )
                group_btn.click()
                group_option_clicked = True
                logger.info(f"  ✅ Clicked 'Share to group': {selector}")
                human_delay(2, 3)
                break
            except Exception as e:
                logger.debug(f"  Group share selector failed: {selector} - {e}")
                continue

        if not group_option_clicked:
            logger.error("❌ [Step 3/6] Could not find 'Share to group' option in share menu")
            # Log visible menu items for debugging
            try:
                menu_items = driver.find_elements(By.XPATH, "//div[@role='menuitem']//span | //div[@role='menu']//span")
                logger.error(f"❌ Menu items visible ({len(menu_items)}):")
                for item in menu_items[:10]:
                    if item.text.strip():
                        logger.error(f"    - '{item.text.strip()}'")
            except Exception:
                pass
            driver.save_screenshot(str(PROJECT_ROOT / "debug" / f"debug_share_{safe_group_name}_error_no_group_option.png"))
            return False

        driver.save_screenshot(str(PROJECT_ROOT / "debug" / f"debug_share_{safe_group_name}_03_group_dialog.png"))
        logger.info(f"📸 Screenshot: debug_share_{safe_group_name}_03_group_dialog.png")

        # --- Step 4: Search for the group ---
        logger.info(f"🔍 [Step 4/6] Looking for group search input...")
        search_selectors = [
            "//input[@placeholder='Szukaj grup']",
            "//input[@placeholder='Search groups']",
            "//input[contains(@placeholder, 'grup')]",
            "//input[contains(@placeholder, 'group')]",
            "//div[@role='dialog']//input[@type='search']",
            "//div[@role='dialog']//input[@type='text']",
        ]

        search_input = None
        for selector in search_selectors:
            try:
                logger.info(f"  🔍 Trying: {selector}")
                search_input = WebDriverWait(driver, 5).until(
                    EC.presence_of_element_located((By.XPATH, selector))
                )
                if search_input:
                    logger.info(f"  ✅ Found search input: {selector}")
                    logger.info(f"  📋 Input placeholder: '{search_input.get_attribute('placeholder')}'")
                    break
            except Exception as e:
                logger.debug(f"  Search selector failed: {selector} - {e}")
                continue

        if not search_input:
            logger.error("❌ [Step 4/6] Could not find group search input")
            # Log all inputs in dialog for debugging
            try:
                inputs = driver.find_elements(By.XPATH, "//div[@role='dialog']//input")
                logger.error(f"❌ Inputs in dialog ({len(inputs)}):")
                for inp in inputs[:5]:
                    logger.error(f"    - type='{inp.get_attribute('type')}', placeholder='{inp.get_attribute('placeholder')}'")
            except Exception:
                pass
            driver.save_screenshot(str(PROJECT_ROOT / "debug" / f"debug_share_{safe_group_name}_error_no_search.png"))
            return False

        logger.info(f"⌨️ Typing search term: '{group_search_name}'")
        search_input.clear()
        search_input.send_keys(group_search_name)
        human_delay(2, 3)

        driver.save_screenshot(str(PROJECT_ROOT / "debug" / f"debug_share_{safe_group_name}_04_search_results.png"))
        logger.info(f"📸 Screenshot: debug_share_{safe_group_name}_04_search_results.png")

        # --- Step 5: Click on the group result ---
        logger.info(f"🔍 [Step 5/6] Looking for group in search results...")
        # Use first 30 chars of group name for matching
        search_fragment = group_search_name[:30]
        group_result_selectors = [
            f"//span[contains(text(), '{search_fragment}')]",
            f"//div[contains(text(), '{search_fragment}')]",
            "//div[@role='listitem']",
            "//div[@role='option']",
        ]

        group_selected = False
        for selector in group_result_selectors:
            try:
                logger.info(f"  🔍 Trying: {selector}")
                group_result = WebDriverWait(driver, 5).until(
                    EC.element_to_be_clickable((By.XPATH, selector))
                )
                # Log what we're about to click
                result_text = group_result.text[:80] if group_result.text else '(no text)'
                logger.info(f"  📋 Element text: '{result_text}'")
                group_result.click()
                group_selected = True
                logger.info(f"  ✅ Selected group from results: {selector}")
                human_delay(2, 3)
                break
            except Exception as e:
                logger.debug(f"  Group result selector failed: {selector} - {e}")
                continue

        if not group_selected:
            logger.error(f"❌ [Step 5/6] Could not select group: {group_search_name}")
            # Log what's visible in the dialog
            try:
                dialog_spans = driver.find_elements(By.XPATH, "//div[@role='dialog']//span")
                logger.error(f"❌ Spans in dialog ({len(dialog_spans)}):")
                for s in dialog_spans[:15]:
                    if s.text.strip():
                        logger.error(f"    - '{s.text.strip()}'")
            except Exception:
                pass
            driver.save_screenshot(str(PROJECT_ROOT / "debug" / f"debug_share_{safe_group_name}_error_no_result.png"))
            return False

        # Wait for "Utwórz post" dialog
        human_delay(2, 3)

        driver.save_screenshot(str(PROJECT_ROOT / "debug" / f"debug_share_{safe_group_name}_05_create_post.png"))
        logger.info(f"📸 Screenshot: debug_share_{safe_group_name}_05_create_post.png")

        # Try to enter caption in textbox
        # NOTE: We need to find the textbox in the "Utwórz post" share dialog specifically,
        # NOT the comment textbox on the underlying post page. The comment box has
        # aria-placeholder="Skomentuj jako..." while the share dialog textbox has
        # aria-placeholder="Powiedz coś o tym..." or similar.
        textbox_selectors = [
            # Share dialog-specific textbox (Polish "Say something about this...")
            "//div[@role='dialog']//div[@role='textbox'][contains(@aria-placeholder, 'Powiedz')]",
            "//div[@role='dialog']//div[@role='textbox'][contains(@aria-placeholder, 'Say something')]",
            # "Utwórz publiczny post" label
            "//div[@aria-label='Utwórz publiczny post…']",
            "//div[@aria-label='Create a public post…']",
            # Generic dialog textbox - but EXCLUDE comment boxes
            "//div[@role='dialog']//div[@role='textbox'][@contenteditable='true'][not(contains(@aria-placeholder, 'Skomentuj'))]",
        ]

        textbox = None
        for selector in textbox_selectors:
            try:
                logger.info(f"  🔍 Trying textbox: {selector}")
                textbox = WebDriverWait(driver, 5).until(
                    EC.presence_of_element_located((By.XPATH, selector))
                )
                if textbox:
                    placeholder = textbox.get_attribute('aria-placeholder') or ''
                    label = textbox.get_attribute('aria-label') or ''
                    logger.info(f"  ✅ Found textbox: {selector}")
                    logger.info(f"  📋 Placeholder: '{placeholder}', Label: '{label}'")
                    break
            except Exception as e:
                logger.debug(f"  Textbox selector failed: {selector} - {e}")
                continue

        if textbox:
            # Use JavaScript click + focus to avoid ElementClickInterceptedException
            # The share dialog textbox can be obscured by overlapping elements
            try:
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", textbox)
                human_delay(0.3, 0.5)
                driver.execute_script("arguments[0].click();", textbox)
                logger.info("  ✅ Clicked textbox via JavaScript")
            except Exception as e:
                logger.warning(f"  ⚠️ JS click on textbox failed: {e}")
            human_delay(0.5, 1)

            logger.info(f"⌨️ Entering caption ({len(caption)} chars)...")
            escaped_caption = caption.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')
            driver.execute_script(f'''
                var el = arguments[0];
                el.focus();
                document.execCommand('insertText', false, "{escaped_caption}");
            ''', textbox)
            logger.info("✅ Caption entered successfully")
            human_delay(1, 2)
        else:
            logger.warning("⚠️ Could not find textbox, posting without caption")
            driver.save_screenshot(str(PROJECT_ROOT / "debug" / f"debug_share_{safe_group_name}_warning_no_textbox.png"))

        driver.save_screenshot(str(PROJECT_ROOT / "debug" / f"debug_share_{safe_group_name}_06_before_publish.png"))
        logger.info(f"📸 Screenshot: debug_share_{safe_group_name}_06_before_publish.png")

        # --- Step 6: Click "Udostępnij" / "Opublikuj" / "Post" ---
        logger.info(f"🔍 [Step 6/6] Looking for Publish/Share button...")
        publish_selectors = [
            # The share dialog uses "Udostępnij" (Share) button, not "Opublikuj" (Publish)
            "//div[@role='dialog']//div[@aria-label='Utwórz post']//span[text()='Udostępnij']",
            "//div[@role='dialog']//span[text()='Udostępnij'][ancestor::div[contains(@class, 'x1qjc9v5')]]",
            "//div[@role='dialog']//span[text()='Opublikuj']",
            "//div[@role='dialog']//span[text()='Post']",
            "//div[@role='dialog']//span[text()='Share']",
            "//div[@aria-label='Opublikuj']",
            "//div[@aria-label='Post']",
        ]

        publish_clicked = False
        for selector in publish_selectors:
            try:
                logger.info(f"  🔍 Trying: {selector}")
                publish_btn = WebDriverWait(driver, 5).until(
                    EC.presence_of_element_located((By.XPATH, selector))
                )
                # Use JavaScript click to avoid intercept issues
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", publish_btn)
                human_delay(0.3, 0.5)
                try:
                    publish_btn.click()
                except Exception:
                    driver.execute_script("arguments[0].click();", publish_btn)
                publish_clicked = True
                logger.info(f"  ✅ Clicked publish: {selector}")
                human_delay(3, 5)
                break
            except Exception as e:
                logger.debug(f"  Publish selector failed: {selector} - {e}")
                continue

        if not publish_clicked:
            logger.error(f"❌ [Step 6/6] Could not find publish button for group: {group_search_name}")
            # Log visible buttons in dialog
            try:
                buttons = driver.find_elements(By.XPATH, "//div[@role='dialog']//div[@role='button']//span")
                logger.error(f"❌ Buttons in dialog ({len(buttons)}):")
                for b in buttons[:10]:
                    if b.text.strip():
                        logger.error(f"    - '{b.text.strip()}'")
            except Exception:
                pass
            driver.save_screenshot(str(PROJECT_ROOT / "debug" / f"debug_share_{safe_group_name}_error_no_publish.png"))
            return False

        driver.save_screenshot(str(PROJECT_ROOT / "debug" / f"debug_share_{safe_group_name}_07_after_publish.png"))
        logger.info(f"📸 Screenshot: debug_share_{safe_group_name}_07_after_publish.png")

        logger.info(f"✅ Successfully shared to group: {group_search_name}")
        logger.info("=" * 50)
        return True

    except Exception as e:
        logger.error(f"❌ Error sharing to group '{group_search_name}': {e}")
        import traceback
        traceback.print_exc()
        try:
            driver.save_screenshot(str(PROJECT_ROOT / "debug" / f"debug_share_{safe_group_name}_error_exception.png"))
            logger.error(f"📸 Screenshot: debug_share_{safe_group_name}_error_exception.png")
        except Exception:
            logger.error("❌ Could not save error screenshot")
        # Try to close any open dialogs to clean up for next group
        try:
            close_btns = driver.find_elements(By.XPATH, "//div[@role='dialog']//div[@aria-label='Zamknij' or @aria-label='Close']")
            for btn in close_btns:
                try:
                    driver.execute_script("arguments[0].click();", btn)
                    logger.info("🧹 Closed open dialog after error")
                    human_delay(0.5, 1)
                except Exception:
                    pass
        except Exception:
            pass
        # Press Escape as a final fallback to close dialogs
        try:
            from selenium.webdriver.common.keys import Keys
            driver.find_element(By.TAG_NAME, 'body').send_keys(Keys.ESCAPE)
            human_delay(0.5, 1)
            logger.info("🧹 Pressed Escape to close any remaining dialogs")
        except Exception:
            pass
        return False


def share_to_all_groups(driver, post_url: str, caption: str) -> int:
    """Share post to all configured Facebook groups.

    Switches to personal profile first (group sharing must be done as personal account),
    then iterates through SHARE_TO_GROUPS with delays between each share.

    Args:
        driver: Selenium WebDriver instance
        post_url: URL of the post to share
        caption: Caption text for shared posts

    Returns:
        Number of successful shares (0 to len(SHARE_TO_GROUPS)).
    """
    if not SHARE_TO_GROUPS_ENABLED:
        logger.info("ℹ️ Group sharing is disabled (SHARE_TO_GROUPS_ENABLED=False)")
        return 0

    if not SHARE_TO_GROUPS:
        logger.info("ℹ️ No groups configured for sharing (SHARE_TO_GROUPS is empty)")
        return 0

    logger.info("=" * 60)
    logger.info(f"📤 STARTING GROUP SHARING: {len(SHARE_TO_GROUPS)} groups configured")
    for i, name in enumerate(SHARE_TO_GROUPS):
        logger.info(f"  {i+1}. {name}")
    logger.info(f"📍 Post URL: {post_url}")
    logger.info(f"⏱️ Delay between shares: {SHARE_DELAY_SECONDS}s (+0-5s random)")
    logger.info("=" * 60)

    # First switch to personal profile (required for group sharing)
    logger.info("🔄 Step 1: Switching to personal profile...")
    if not switch_to_personal_profile(driver):
        logger.error("❌ Could not switch to personal profile, aborting all group shares")
        logger.error("❌ This means we're still logged in as the page, which cannot share to groups")
        return 0

    successful_shares = 0
    failed_groups = []

    for i, group_name in enumerate(SHARE_TO_GROUPS):
        logger.info(f"--- Group {i+1}/{len(SHARE_TO_GROUPS)}: {group_name} ---")

        if share_post_to_group(driver, post_url, group_name, caption):
            successful_shares += 1
            logger.info(f"✅ [{i+1}/{len(SHARE_TO_GROUPS)}] Shared to: {group_name}")
        else:
            failed_groups.append(group_name)
            logger.error(f"❌ [{i+1}/{len(SHARE_TO_GROUPS)}] Failed to share to: {group_name}")

        # Delay between shares (except for last one)
        if i < len(SHARE_TO_GROUPS) - 1:
            delay = SHARE_DELAY_SECONDS + random.uniform(0, 5)
            logger.info(f"⏳ Waiting {delay:.0f}s before next group share...")
            time.sleep(delay)

    logger.info("=" * 60)
    logger.info(f"📊 GROUP SHARING SUMMARY:")
    logger.info(f"  Total groups: {len(SHARE_TO_GROUPS)}")
    logger.info(f"  Successful: {successful_shares}")
    logger.info(f"  Failed: {len(failed_groups)}")
    if failed_groups:
        logger.info(f"  Failed groups: {failed_groups}")
    logger.info("=" * 60)

    return successful_shares


# ============================================
# MAIN
# ============================================

def main():
    # ============================================
    # ACQUIRE SCRIPT LOCK
    # ============================================
    if not acquire_script_lock():
        logger.error("❌ Another instance is already running. Exiting.")
        sys.exit(1)

    # Register cleanup handlers
    atexit.register(release_script_lock)

    # ============================================
    # START VIRTUAL DISPLAY IF CONFIGURED
    # ============================================
    virtual_display = None
    if USE_VIRTUAL_DISPLAY:
        try:
            from pyvirtualdisplay import Display
            virtual_display = Display(visible=False, size=(1920, 1080))
            virtual_display.start()
            logger.info("🖥️ Started virtual display (Xvfb)")
        except ImportError:
            logger.error("❌ pyvirtualdisplay not installed! Run: pip install pyvirtualdisplay")
            return
        except Exception as e:
            logger.error(f"❌ Failed to start virtual display: {e}")
            return

    try:
        import datetime as _dt
        start_time = time.time()
        logger.info("=" * 60)
        logger.info(">>> Rozpoczynam generowanie mapy pogodowej Boguszów-Gorce (Selenium)...")
        logger.info(f">>> Start time: {_dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info(f">>> TEST_MODE={TEST_MODE}, USE_VIRTUAL_DISPLAY={USE_VIRTUAL_DISPLAY}")
        logger.info(f">>> SHARE_TO_GROUPS_ENABLED={SHARE_TO_GROUPS_ENABLED}")
        logger.info(f">>> SHARE_TO_GROUPS={SHARE_TO_GROUPS}")
        logger.info(f">>> FB_PAGE_URL={FB_PAGE_URL}")
        logger.info(f">>> PERSONAL_PROFILE_NAME={PERSONAL_PROFILE_NAME}")
        logger.info(f">>> MAPS_DIR={MAPS_DIR}")
        logger.info(f">>> OVERLAY_ENABLED={OVERLAY_ENABLED}")
        if TEST_MODE:
            logger.info(">>> 🧪 TRYB TESTOWY - cały pipeline będzie przetestowany, ale post NIE zostanie opublikowany")
        logger.info("=" * 60)

        # 1. Fetch weather data for all 7 districts
        districts_weather = fetch_districts_weather()
        if not districts_weather:
            logger.error("Brak danych pogodowych. Przerywam.")
            return

        # 2. Fetch forecast for center (Boguszów-Gorce) and generate professional forecast
        forecast = fetch_forecast_center()

        mode = forecast.get('forecast_mode', 'day') if forecast else 'day'

        try:
            if forecast and forecast.get('hourly') and forecast['hourly'].get('temps'):
                # Use professional forecast
                forecast_text = generate_professional_forecast_text(forecast['hourly'], mode)
                logger.info("✅ Using professional meteorological forecast")
            elif forecast:
                # Fallback to simple forecast
                forecast_text = generate_forecast_text(forecast)
                logger.info("⚠️ Using simple fallback forecast")
            else:
                forecast_text = "Sprawdź temperaturę w swojej dzielnicy na mapie."
                logger.warning("⚠️ No forecast data available")
        except Exception as e:
            logger.error(f"❌ Forecast generation error: {e}")
            forecast_text = "Sprawdź temperaturę w swojej dzielnicy na mapie."

        # 3. Get weather code for map selection (Boguszów-Gorce center - index 3)
        weather_code = districts_weather[3]['code'] if len(districts_weather) > 3 else 3
        logger.info(f"📊 Kod pogody dla Boguszowa-Gorc: {weather_code} (mode: {mode})")

        # 4. Generate map image (with appropriate weather icon map)
        map_path, min_t, max_t = generate_map_image(districts_weather, weather_code, mode)

        if not map_path:
            logger.error("Nie udało się wygenerować mapy. Przerywam.")
            return

        # 5. Prepare caption
        desc = "Pochmurno"
        if weather_code in [0, 1]: desc = "Pogodnie"
        elif weather_code in [2, 3]: desc = "Pochmurno"
        elif weather_code in [45, 48]: desc = "Mgliście"
        elif weather_code >= 51 and weather_code <= 67: desc = "Opady deszczu"
        elif weather_code >= 71 and weather_code <= 86: desc = "Opady śniegu"

        if min_t == max_t:
            range_str = format_temp(min_t)
        else:
            range_str = f"od {format_temp(min_t)} do {format_temp(max_t)}"

        caption = f"""🌡️ Aktualna temperatura w Boguszowie-Gorcach: {range_str}. {desc}.
{forecast_text}

❤️ Mieszkańcu Boguszowa-Gorc — możesz wesprzeć lokalną fundację.
👉 To nic Cię nie kosztuje. KRS: 0000498479

Więcej: {FB_PROFILE_LINK}

#BoguszówGorce #Boguszów #DolnyŚląsk"""

        logger.info(f"Treść posta:\n{caption}")

        # 6. Post to Facebook using Selenium (full pipeline, but don't publish if TEST_MODE)
        driver = None
        try:
            logger.info("🚀 Starting Chrome browser...")
            driver = setup_chrome_driver_with_retry()

            # Ensure we're logged in as the page (3-stage approach)
            if not ensure_logged_in_as_page(driver):
                logger.error("❌ Could not verify page login")
                return

            # Post the weather map (pass test_mode flag)
            success = post_to_facebook_selenium(driver, map_path, caption, test_mode=TEST_MODE)

            if success:
                if TEST_MODE:
                    logger.info("🧪 TEST MODE: Pipeline test completed successfully!")
                else:
                    logger.info("🎉 Post opublikowany pomyślnie!")

                    # Share to groups (only in production mode after successful post)
                    if SHARE_TO_GROUPS_ENABLED and SHARE_TO_GROUPS:
                        logger.info("=" * 60)
                        logger.info("📤 Starting group sharing phase...")
                        logger.info(f"📤 Groups to share to: {len(SHARE_TO_GROUPS)}")
                        logger.info("=" * 60)

                        # Get the URL of the post we just created
                        post_url = get_latest_post_url(driver)
                        logger.info(f"📍 Post URL for sharing: {post_url}")

                        # Prepare group share caption
                        group_caption = caption  # Use same caption as the page post

                        # Share to all configured groups
                        shares_count = share_to_all_groups(driver, post_url, group_caption)

                        if shares_count > 0:
                            logger.info(f"🎉 Successfully shared to {shares_count}/{len(SHARE_TO_GROUPS)} groups!")
                        else:
                            logger.warning(f"⚠️ No successful group shares (0/{len(SHARE_TO_GROUPS)})")
                    else:
                        logger.info("ℹ️ Group sharing skipped (disabled or no groups configured)")
            else:
                logger.error("❌ Nie udało się opublikować posta")

        except Exception as e:
            logger.error(f"❌ Critical error: {e}")
            import traceback
            traceback.print_exc()
            if driver:
                driver.save_screenshot(str(PROJECT_ROOT / "debug" / "debug_critical_error.png"))
        finally:
            if driver:
                human_delay(2, 3)
                logger.info("Closing browser...")
                driver.quit()
                logger.info("✅ Browser closed")

    finally:
        # ============================================
        # STOP VIRTUAL DISPLAY
        # ============================================
        if virtual_display:
            virtual_display.stop()
            logger.info("🖥️ Stopped virtual display")

        try:
            elapsed = time.time() - start_time
            logger.info("=" * 60)
            logger.info(f">>> FINISHED: Total runtime {elapsed:.0f}s ({elapsed/60:.1f} min)")
            logger.info(f">>> End time: {_dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            logger.info("=" * 60)
        except Exception:
            logger.info(">>> FINISHED")


if __name__ == "__main__":
    main()
