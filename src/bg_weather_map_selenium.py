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
]
SHARE_DELAY_MIN = 45   # Min delay between group shares (seconds)
SHARE_DELAY_MAX = 120  # Max delay between group shares (seconds)
MAX_GROUPS_PER_RUN = 5  # Max groups to share to per run (0 = unlimited)
PERSONAL_PROFILE_NAME = "Piotr Kirklewski"

# ============================================
# RCB ALERT — EXTRA PROFILE-WALL DISTRIBUTION
# ============================================
# Profiles/professional-accounts that should receive the IMGW alert post
# pasted onto their wall via "Napisz coś do <name>..." textbox.
#
# ⚠️ ALERT-ONLY — these profiles MUST NEVER receive regular daily weather
# or city-news posts. Local services (e.g. Straż Miejska) have agreed to
# host alert notifications but would BAN US for spam if we posted daily
# content there.
#
# Safety architecture:
#   - This list is consumed ONLY by post_alert_to_profile_wall() which
#     enforces an `is_alert=True` guard parameter.
#   - The regular share_to_all_groups() pipeline reads ONLY from
#     SHARE_TO_GROUPS and never sees this list.
#   - bg's main() (regular daily cron) does NOT iterate this list.
#   - When alert-mode publishing is added to main(), the alert path
#     must call post_alert_to_profile_wall(..., is_alert=True)
#     explicitly for each entry here.
RCB_ALERT_EXTRA_PROFILE_POSTS = [
    # Straż Miejska Boguszów-Gorce — public service profile, asked to
    # receive RCB-class meteo alerts (storms, heat, frost, etc.).
    "https://www.facebook.com/profile.php?id=100065918171599",
]

# FAST_MODE — production capability for ad-hoc / emergency runs.
# Activated via env var WMAP_FAST_MODE=1. When True:
#   - human_delay() collapses to near-zero (skips anti-detection pauses
#     between Selenium UI actions)
#   - generate_map_image() reuses existing output PNG if it exists and
#     is fresh enough (see FAST_MODE_REUSE_MAX_AGE_SEC) — skips re-fetch
#     of all districts + IMGW + image rendering
# DOES NOT shorten SHARE_DELAY_MIN/MAX (those are anti-spam, removing
# them risks FB flagging the account).
FAST_MODE = os.environ.get("WMAP_FAST_MODE", "0") == "1"
FAST_MODE_REUSE_MAX_AGE_SEC = 3 * 3600  # reuse output PNG if <3 h old

# ============================================
# IMGW METEO WARNINGS (RCB-equivalent source)
# ============================================
# Powiat TERYT code(s) covering Boguszów-Gorce. Verified via Nominatim
# reverse-geocode of all 7 DISTRICTS — all fall in powiat wałbrzyski (0221).
IMGW_TERYT_CODES = ["0221"]
IMGW_WARNINGS_URL = "https://danepubliczne.imgw.pl/api/data/warningsmeteo/teryt/{teryt}"

# RCB alert visual config (mirror of wch). The alert image is composed as:
#   map_storm.png (base) + stormRCB2transpartentBCKG.png (overlay at
#   position below) + scarlet temps + banerTopRCB.png (broadcast banner
#   on top) + dynamic warning cards + drop shadow under banner block.
RCB_ALERT_TEMP_COLOR = (255, 36, 0)            # Scarlet #FF2400
RCB_ALERT_BANNER_FILE = "banerTopRCB.png"      # broadcast-style alert banner
RCB_ALERT_STORM_BASE = "map_storm.png"         # base map (with city outline)
RCB_ALERT_STORM_OVERLAY = "stormRCB2transpartentBCKG.png"  # dramatic storm cloud
# Overlay positioning iterated with user 2026-06-20 (v5 was final):
RCB_OVERLAY_W = 640
RCB_OVERLAY_X = 680
RCB_OVERLAY_Y = -90  # slight negative — overlay extends above map top
RCB_CARD_BG = (255, 248, 232)                   # cream cards background
RCB_CARD_INK = (24, 24, 24)
RCB_CARD_INK_SOFT = (60, 60, 60)
RCB_CARD_ACCENT_RED = (192, 57, 43)             # stopień 2/3 accent stripe
RCB_CARD_ACCENT_YELLOW = (245, 195, 0)          # stopień 1 accent stripe
RCB_CARD_SEPARATOR = (215, 175, 100)
RCB_CARDS_HEIGHT = 110
RCB_SHADOW_HEIGHT = 22
RCB_SHADOW_MAX_ALPHA = 180

# Banner colors by stopień (1=informacyjne, 2=ostrzeżenie, 3=alarm)
WARNING_STOPIEN_COLOR = {
    "1": (255, 213, 3),    # Yellow #FFD503 (matches our temp palette)
    "2": (253, 171, 19),   # Orange #FDAB13 (matches our temp palette)
    "3": (220, 50, 50),    # Red for danger
}
WARNING_EVENT_EMOJI = {
    "Burze": "⛈", "Burze z gradem": "⛈", "Trąby powietrzne": "🌪",
    "Upał": "🔥", "Mróz": "❄", "Silny mróz": "❄",
    "Silny wiatr": "🌬", "Wiatr": "🌬",
    "Intensywne opady deszczu": "🌧", "Opady deszczu": "🌧",
    "Intensywne opady śniegu": "❄", "Opady śniegu": "❄",
    "Mgła": "🌫", "Oblodzenie": "🧊", "Roztopy": "💧",
}

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
    """Random delay to mimic human behavior.

    In FAST_MODE, uses a small fixed delay (~0.3 s) instead of the random
    0.5-2 s. Trade-off: still ~5x faster than baseline, but enough for FB's
    React-driven UI to finish rendering before the next Selenium click —
    a tighter 0.05 s caused "element click intercepted by other element"
    errors during caption entry in early FAST_MODE testing.
    """
    if FAST_MODE:
        time.sleep(0.3)
        return
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
    elif temp < 25: return (249, 166, 2)     # Gold Saffron #F9A602 (warm)
    elif temp < 30: return (255, 140, 0)     # Tangerine #FF8C00 (hot)
    return (255, 36, 0)                      # Scarlet #FF2400 (extreme — matches RCB alert)

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
        tuple: (forecast_text, short_desc) - Professional Polish forecast text and short weather description
    """
    if not hourly_data or not hourly_data.get('temps'):
        logger.warning("⚠️ No hourly data for professional forecast, using fallback")
        return "Sprawdź temperaturę w swojej dzielnicy na mapie.", None

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

    # === SHORT DESCRIPTION for caption header ===
    # Derived from the same forecast data to avoid contradicting the forecast body
    if max_precip > 40 and avg_code >= 71:
        short_desc = "Opady śniegu"
    elif max_precip > 40 and avg_code >= 51:
        short_desc = "Opady deszczu"
    elif avg_code in [45, 48]:
        short_desc = "Mgliście"
    elif avg_code <= 1:
        short_desc = "Pogodnie"
    elif avg_code <= 3:
        short_desc = "Pochmurno"
    else:
        short_desc = "Pochmurno"

    return "".join(parts), short_desc


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


# ============================================
# IMGW WARNINGS FETCH & RENDER
# ============================================

def fetch_imgw_warnings(teryt_codes: list) -> list:
    """Fetch active IMGW meteo warnings for one or more powiat TERYT codes.

    Returns deduplicated list (by warning id), filtered to warnings whose
    `obowiazuje_do` is still in the future, sorted by stopień DESC then start.
    Empty list on any failure — we never want to block map publishing on this.
    """
    seen_ids = set()
    raw = []
    for code in teryt_codes:
        try:
            r = requests.get(IMGW_WARNINGS_URL.format(teryt=code), timeout=8)
            if not r.ok:
                logger.warning(f"⚠️ IMGW HTTP {r.status_code} for teryt={code}")
                continue
            data = r.json()
            if not isinstance(data, list):
                continue
            for w in data:
                wid = w.get('id')
                if wid in seen_ids:
                    continue
                seen_ids.add(wid)
                raw.append(w)
        except Exception as e:
            logger.warning(f"⚠️ IMGW fetch failed for teryt={code}: {e}")

    now = datetime.now()
    active = []
    for w in raw:
        try:
            d_to = datetime.strptime(w['obowiazuje_do'], "%Y-%m-%d %H:%M:%S")
            if d_to >= now:
                active.append(w)
        except Exception:
            active.append(w)  # if parse fails, include defensively
    active.sort(key=lambda w: (-int(w.get('stopien', 0) or 0), w.get('obowiazuje_od', '')))
    logger.info(f"🚨 IMGW warnings active for {teryt_codes}: {len(active)} "
                f"({[w.get('nazwa_zdarzenia') + ' st.' + str(w.get('stopien')) for w in active]})")
    return active


def draw_warnings_banner(image_path: str, warnings: list) -> str:
    """Add a colored warning banner to the top of the map image.

    Mutates the file at `image_path` in place. Returns the same path.
    No-op (and no file change) if `warnings` is empty.
    Banner color = highest stopień among active warnings.
    """
    if not warnings:
        return image_path
    try:
        img = Image.open(image_path).convert('RGBA')
        w, h = img.size

        max_stopien = max((int(x.get('stopien', 1) or 1) for x in warnings), default=2)
        bg_color = WARNING_STOPIEN_COLOR.get(str(max_stopien), (253, 171, 19))

        # Banner height: ~9% of map height, clamped sensibly
        banner_h = max(120, min(180, int(h * 0.09)))

        # Compose: banner on top, original map below
        new_img = Image.new('RGBA', (w, h + banner_h), bg_color + (255,))
        new_img.paste(img, (0, banner_h))
        draw = ImageDraw.Draw(new_img)

        title_font = get_font(42, bold=True)
        line_font = get_font(26, bold=True)
        small_font = get_font(20, bold=True)

        title = "⚠ OSTRZEŻENIE METEO IMGW"
        draw.text((20, 12), title, font=title_font, fill=(0, 0, 0))

        # Up to 2 warning lines (most severe first)
        y = 62
        for wd in warnings[:2]:
            name = wd.get('nazwa_zdarzenia', '?')
            st = wd.get('stopien', '?')
            emoji = WARNING_EVENT_EMOJI.get(name, "⚠")
            d_from = (wd.get('obowiazuje_od', '') or '')[5:16]  # "MM-DD HH:MM"
            d_to = (wd.get('obowiazuje_do', '') or '')[5:16]
            line = f"{emoji} {name} (stopień {st})   {d_from} → {d_to}"
            draw.text((20, y), line, font=line_font, fill=(0, 0, 0))
            y += 30

        # Source line bottom-right of banner
        src = "Źródło: IMGW-PIB"
        bbox = draw.textbbox((0, 0), src, font=small_font)
        sw = bbox[2] - bbox[0]
        draw.text((w - sw - 20, banner_h - 28), src, font=small_font, fill=(0, 0, 0))

        new_img.save(image_path, 'PNG')
        logger.info(f"✅ Warning banner added to {image_path} ({banner_h}px, color stopień {max_stopien})")
        return image_path
    except Exception as e:
        logger.error(f"❌ Could not draw warnings banner: {e}")
        return image_path


def format_warnings_for_caption(warnings: list) -> str:
    """Format warnings as a caption header. Returns '' if no warnings."""
    if not warnings:
        return ""
    lines = ["⚠️ OSTRZEŻENIE METEO IMGW:"]
    for wd in warnings:
        name = wd.get('nazwa_zdarzenia', '?')
        st = wd.get('stopien', '?')
        prob = wd.get('prawdopodobienstwo', '')
        d_from = wd.get('obowiazuje_od', '')
        d_to = wd.get('obowiazuje_do', '')
        emoji = WARNING_EVENT_EMOJI.get(name, "⚠")
        prob_str = f", prawdopodobieństwo {prob}%" if prob else ""
        lines.append(f"{emoji} {name} — stopień {st}{prob_str}")
        lines.append(f"   Obowiązuje: {d_from} → {d_to}")
        tresc = (wd.get('tresc') or '').strip()
        if tresc:
            # Trim to keep caption manageable
            snippet = tresc[:260] + ("…" if len(tresc) > 260 else "")
            lines.append(f"   {snippet}")
    return "\n".join(lines)


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


def _derive_verification_needle(caption: str) -> str:
    """Pick a distinctive ~30-50 char substring from caption to verify a post
    is actually rendered on the page after publish.

    Prefers the 'Aktualna temperatura' line (freshly generated each run with
    current temp range — almost never collides with older posts). Falls back
    to first substantive prose line, then to first 50 chars.

    Callers should prefer passing an explicit verify_needle to
    post_to_facebook_selenium when they have unique-per-run data that's
    guaranteed to render ABOVE FB's "... Wyświetl więcej" caption truncation
    point (≈ first 80-100 chars).
    """
    for line in caption.split('\n'):
        s = line.strip()
        if 'Aktualna temperatura' in s:
            return s.lstrip('🌡️ ').strip()[:60]
    SKIP_LEADS = ('•', '#', '📍', 'Więcej:', 'KRS:')
    SKIP_EMOJI_PREFIX = '⚠️🌡️❤️👉🚨📍'
    for line in caption.split('\n'):
        s = line.strip()
        if not s or any(s.startswith(lead) for lead in SKIP_LEADS):
            continue
        for ch in list(SKIP_EMOJI_PREFIX):
            if s.startswith(ch):
                s = s[len(ch):].lstrip()
        if 25 <= len(s) <= 80 and any(c.isalpha() for c in s):
            return s[:50]
    return caption.replace('\n', ' ').strip()[:50]


def verify_post_published_and_get_url(driver, verify_needle: str,
                                       timeout: int = 15) -> str:
    """STRICT post-publish verification.

    Navigates to FB_PAGE_URL and looks for verify_needle in the rendered DOM.
    If found, extracts the specific post's permalink URL. Two layout modes
    are supported:

    (1) Public view → direct /posts/<id> or /permalink/ anchors
    (2) Page-admin view (logged in as page) → no direct permalink anchors;
        reconstruct facebook.com/<page_id>/posts/<post_id> from the
        target_id + page_id query params of the boost-post URL that admin
        view DOES render on each post.

    Returns the post URL string on success, None on failure. Callers MUST
    treat None as "publish actually failed" and skip downstream sharing —
    falling back to FB_PAGE_URL is the historical 2026-06-20 bug that
    caused 5 wch groups to receive an unrelated TVWALBRZYCH 'Riese'
    article reshare with our IMGW alert caption attached.
    """
    try:
        logger.info(f"🔎 Verifying post on page — needle: {verify_needle!r}")
        driver.get(FB_PAGE_URL)
        human_delay(4, 6)

        if "'" in verify_needle and '"' in verify_needle:
            logger.warning("⚠️ Needle contains both quote types — using partial match")
            verify_needle = verify_needle.replace("'", " ").replace('"', ' ')
        xpath_lit = f'"{verify_needle}"' if "'" in verify_needle else f"'{verify_needle}'"

        try:
            needle_el = WebDriverWait(driver, timeout).until(
                EC.presence_of_element_located(
                    (By.XPATH, f"//*[contains(text(), {xpath_lit})]")
                )
            )
            logger.info("✅ Needle found on page")
        except TimeoutException:
            logger.error(f"❌ Post verification FAILED — needle {verify_needle!r} "
                         f"not present on page within {timeout}s after publish.")
            driver.save_screenshot(str(PROJECT_ROOT / "debug" / "debug_verify_needle_missing.png"))
            return None

        import re
        TARGET_ID_RE = re.compile(r'[?&]target_id=(\d+)')
        PAGE_ID_RE = re.compile(r'[?&]page_id=(\d+)')

        for levels in range(1, 16):
            try:
                xpath_up = "./" + ("../" * levels) + "."
                ancestor = needle_el.find_element(By.XPATH, xpath_up)
                all_links = ancestor.find_elements(By.XPATH, ".//a[@href]")
                for link in all_links:
                    href = link.get_attribute('href') or ''
                    if any(skip in href for skip in ('comment_id', 'notif_id', '/groups/',
                                                     '/ad_center/', '/photo/')):
                        continue
                    if any(k in href for k in ('/posts/', '/permalink/', 'permalink.php')):
                        clean_url = href.split('&__tn__')[0].split('&__cft__')[0]
                        logger.info(f"✅ Post permalink extracted (direct): {clean_url}")
                        return clean_url
                target_id = page_id = None
                for link in all_links:
                    href = link.get_attribute('href') or ''
                    if '/ad_center/' in href and 'target_id=' in href:
                        m_t = TARGET_ID_RE.search(href)
                        m_p = PAGE_ID_RE.search(href)
                        if m_t and m_p:
                            target_id, page_id = m_t.group(1), m_p.group(1)
                            break
                if target_id and page_id:
                    reconstructed = f"https://www.facebook.com/{page_id}/posts/{target_id}"
                    logger.info(f"✅ Post permalink reconstructed from admin-view IDs: {reconstructed}")
                    return reconstructed
            except Exception:
                continue

        logger.error("❌ Needle found but no permalink derivable. "
                     "Refusing to share to avoid wrong-post fallback.")
        driver.save_screenshot(str(PROJECT_ROOT / "debug" / "debug_verify_no_permalink.png"))
        return None
    except Exception as e:
        logger.error(f"❌ verify_post_published_and_get_url crashed: {e}")
        import traceback; traceback.print_exc()
        return None


def post_to_facebook_selenium(driver, image_path: str, caption: str,
                               verify_needle: str = None,
                               test_mode: bool = False) -> tuple:
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
                return True, None

            # ============================================
            # PRODUCTION MODE: Actually publish
            # ============================================
            if final_btn:
                human_delay(0.5, 1)
                final_btn.click()
                logger.info("✅ Clicked final Opublikuj!")

            human_delay(2, 3)

            # Handle FB post-publish upsell dialogs. Critical: FB sometimes
            # intercepts the publish click with a "Organizujesz wydarzenie?"
            # promotion dialog whose [Opublikuj oryginalny post] button is the
            # one that actually publishes. Without dismissing it, the post
            # stays in limbo (NOT on the page feed). Always try the publish-
            # original dismiss FIRST, then fall back to "Not now"-style buttons.
            popup_handled = False
            popup_selectors = [
                "//span[text()='Opublikuj oryginalny post']",
                "//div[@role='button']//span[text()='Opublikuj oryginalny post']",
                "//span[text()='Publish original post']",
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

            # STRICT verification — see wch 2026-06-20 incident notes. If we
            # log success without confirming the post is actually rendered
            # on the page, downstream share_to_all_groups will use FB_PAGE_URL
            # which renders the page-admin view and clicks "Udostępnij" on
            # an arbitrary widget-panel post → wrong content propagated to N
            # groups. Refuse to report success unless we find a unique needle
            # AND extract the specific post permalink.
            needle = verify_needle or _derive_verification_needle(caption)
            post_url = verify_post_published_and_get_url(driver, needle)
            if not post_url:
                logger.error("❌ Post NOT verified on page — refusing to report success "
                             "to prevent downstream wrong-post sharing.")
                return False, None

            logger.info(f"✅ Post published AND verified on page!  URL: {post_url}")
            return True, post_url
        else:
            logger.error("❌ Could not find publish button")
            driver.save_screenshot(str(PROJECT_ROOT / "debug" / "debug_no_publish.png"))
            return False, None

    except Exception as e:
        logger.error(f"❌ Selenium error: {e}")
        import traceback
        traceback.print_exc()
        driver.save_screenshot(str(PROJECT_ROOT / "debug" / "debug_error.png"))
        return False, None


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
                        # Clean up tracking params, but keep essential query params
                        # for permalink.php URLs (story_fbid/id are required)
                        if '?' in href and 'permalink.php' not in href:
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


def close_share_dialog(driver):
    """Attempt to close any open share dialog after a failed/rate-limited share."""
    try:
        close_btns = driver.find_elements(By.XPATH, "//div[@role='dialog']//div[@aria-label='Zamknij' or @aria-label='Close']")
        for btn in close_btns:
            try:
                driver.execute_script("arguments[0].click();", btn)
                logger.info("🧹 Closed open share dialog")
                human_delay(0.5, 1)
                return
            except Exception:
                pass
    except Exception:
        pass
    # Fallback: press Escape
    try:
        from selenium.webdriver.common.keys import Keys
        driver.find_element(By.TAG_NAME, 'body').send_keys(Keys.ESCAPE)
        human_delay(0.5, 1)
        logger.info("🧹 Pressed Escape to close dialog")
    except Exception:
        pass


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
        for attempt in range(3):
            for selector in share_selectors:
                try:
                    logger.info(f"  🔍 Trying: {selector}")
                    share_btn = WebDriverWait(driver, 5).until(
                        EC.element_to_be_clickable((By.XPATH, selector))
                    )
                    try:
                        share_btn.click()
                    except Exception:
                        driver.execute_script("arguments[0].click();", share_btn)
                    share_clicked = True
                    logger.info(f"  ✅ Clicked share button: {selector}")
                    human_delay(2, 3)
                    break
                except Exception as e:
                    logger.debug(f"  Share selector failed: {selector} - {e}")
                    continue
            if share_clicked:
                break
            if attempt < 2:
                logger.info(f"  🔍 [Step 2/6] Share button not found, scrolling down (attempt {attempt + 1}/3)...")
                driver.execute_script("window.scrollBy(0, 400);")
                human_delay(1, 2)

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

        # DEFENSE IN DEPTH: verify the share dialog actually contains an embed
        # of OUR page's post. We detect this by looking for FB_PAGE_NAME in the
        # dialog text — that name is rendered by the embed-card (page that
        # authored the original post) but NOT present in our typed caption
        # (the caption uses FB_PROFILE_LINK URL only). If FB_PAGE_NAME is
        # missing, this dialog is sharing somebody else's post — abort.
        try:
            dialog_el = driver.find_element(By.XPATH, "//div[@role='dialog']")
            dialog_text = dialog_el.text or ""
            if FB_PAGE_NAME not in dialog_text:
                logger.error(f"❌ PRE-PUBLISH GUARD: share dialog does NOT contain "
                             f"'{FB_PAGE_NAME}' — embed is from a different page. "
                             f"Aborting share to {group_search_name} to prevent "
                             f"wrong-content propagation (wch 2026-06-20 bug).")
                driver.save_screenshot(str(PROJECT_ROOT / "debug" /
                    f"debug_share_{safe_group_name}_WRONG_EMBED_ABORTED.png"))
                return False
            logger.info(f"✅ Pre-publish guard passed: embed is from '{FB_PAGE_NAME}'")
        except Exception as e:
            logger.warning(f"⚠️ Pre-publish guard could not inspect dialog ({e}); proceeding")

        # --- Step 6: Click submit button (FB share-to-group dialog uses
        #     "Opublikuj" as the main blue submit button at the bottom).
        #     IMPORTANT: try "Opublikuj" FIRST. Previously this list led with
        #     "Udostępnij" inside an obfuscated x1qjc9v5 class — that matched
        #     a SECONDARY share-count link on the embedded post preview, not
        #     the real submit button. Result: click went through (script
        #     logged success), but no actual share was published.
        #     (Verified 2026-06-20: bg group "BOGUSZÓW-GORCE" did NOT receive
        #     the alert share even though guards passed, until this fix.)
        logger.info(f"🔍 [Step 6/6] Looking for Publish/Share button...")
        publish_selectors = [
            "//div[@role='dialog']//div[@role='button']//span[text()='Opublikuj']",
            "//div[@role='dialog']//span[text()='Opublikuj']",
            "//div[@aria-label='Opublikuj' and @role='button']",
            "//div[@role='dialog']//div[@role='button']//span[text()='Post']",
            "//div[@role='dialog']//span[text()='Post']",
            # Last resort — the older "Udostępnij" fallbacks (kept for older
            # group dialog variants that submit with this text):
            "//div[@role='dialog']//div[@aria-label='Utwórz post']//span[text()='Udostępnij']",
            "//div[@role='dialog']//span[text()='Udostępnij'][ancestor::div[contains(@class, 'x1qjc9v5')]]",
            "//div[@role='dialog']//span[text()='Share']",
            "//div[@aria-label='Opublikuj']",
            "//div[@aria-label='Post']",
        ]

        publish_clicked = False
        for selector in publish_selectors:
            try:
                logger.info(f"  🔍 Trying: {selector}")
                publish_btn = WebDriverWait(driver, 5).until(
                    EC.element_to_be_clickable((By.XPATH, selector))
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

        # --- Verify share completed: wait for dialog to disappear ---
        logger.info(f"  ⏳ Waiting for share dialog to close (confirming share)...")
        try:
            WebDriverWait(driver, 15).until(
                EC.invisibility_of_element_located((By.XPATH, "//div[@role='dialog']//div[@aria-label='Utwórz post']"))
            )
            # Dialog gone = share completed
            driver.save_screenshot(str(PROJECT_ROOT / "debug" / f"debug_share_{safe_group_name}_07_after_publish.png"))
            logger.info(f"📸 Screenshot: debug_share_{safe_group_name}_07_after_publish.png")
            logger.info(f"✅ Share dialog closed — share confirmed for: {group_search_name}")
            logger.info("=" * 50)
            return True
        except TimeoutException:
            # Dialog still open after 15s = rate-limited or error
            logger.warning(f"⚠️ Share dialog still open after publish — possible rate limit for: {group_search_name}")
            # Check for error/rate-limit text in dialog
            try:
                error_texts = driver.find_elements(By.XPATH,
                    "//div[@role='dialog']//*[contains(text(), 'limit') or contains(text(), 'błąd') or contains(text(), 'error') or contains(text(), 'spróbuj') or contains(text(), 'try again')]")
                if error_texts:
                    for et in error_texts[:3]:
                        if et.text.strip():
                            logger.error(f"  🚫 Rate limit text found: '{et.text.strip()}'")
            except Exception:
                pass
            driver.save_screenshot(str(PROJECT_ROOT / "debug" / f"debug_share_{safe_group_name}_error_rate_limit.png"))
            logger.error(f"📸 Screenshot: debug_share_{safe_group_name}_error_rate_limit.png")
            close_share_dialog(driver)
            return False

    except Exception as e:
        logger.error(f"❌ Error sharing to group '{group_search_name}': {e}")
        import traceback
        traceback.print_exc()
        try:
            driver.save_screenshot(str(PROJECT_ROOT / "debug" / f"debug_share_{safe_group_name}_error_exception.png"))
            logger.error(f"📸 Screenshot: debug_share_{safe_group_name}_error_exception.png")
        except Exception:
            logger.error("❌ Could not save error screenshot")
        close_share_dialog(driver)
        return False


# ============================================
# RCB ALERT — POST TO PROFILE WALL
# ============================================

def post_alert_to_profile_wall(driver, profile_url: str, our_post_url: str,
                                is_alert: bool) -> bool:
    """Post our alert URL on a target profile's wall via "Napisz coś do <X>..."

    Used to distribute RCB-class meteo alerts to local services (e.g. Straż
    Miejska) that maintain a public profile and explicitly accept alert
    notifications. FB auto-renders the pasted URL as a rich embedded post
    preview — no moderator approval needed (unlike groups).

    ⚠️ HARD GUARD: requires `is_alert=True`. This is the third independent
    safety layer (alongside RCB_ALERT_EXTRA_PROFILE_POSTS being a separate
    config list and not being touched by regular share_to_all_groups). The
    target profiles have agreed to host alerts; pushing daily city-news
    posts here would get us banned.

    Args:
        driver: Selenium WebDriver
        profile_url: FB profile URL of the alert recipient
        our_post_url: URL of the alert post on our page (the one being shared)
        is_alert: MUST be True. Raises if False, to make accidental misuse
                  from regular-cron contexts impossible.

    Returns:
        True if post was published on the profile wall, False otherwise.
    """
    if not is_alert:
        raise ValueError(
            "post_alert_to_profile_wall called with is_alert=False — refusing. "
            "These profiles are ALERT-ONLY recipients and posting daily content "
            "to them would result in a ban. If you genuinely need this for a "
            "non-alert run (testing only), pass is_alert=True explicitly with "
            "a comment, but be aware: it WILL post on Straż Miejska's wall."
        )

    safe_name = "".join(c if c.isalnum() else "_" for c in profile_url[-20:])

    try:
        logger.info("=" * 50)
        logger.info(f"📤 POSTING ALERT TO PROFILE WALL: {profile_url}")
        logger.info(f"📍 Alert post URL: {our_post_url}")
        logger.info("=" * 50)

        driver.get(profile_url)
        human_delay(3, 5)
        driver.save_screenshot(str(PROJECT_ROOT / "debug" / f"debug_wallpost_{safe_name}_01_profile_loaded.png"))

        # --- Step 1: Click the "Napisz coś do <name>..." entry area to open
        #     the post composer dialog.
        composer_entry_selectors = [
            "//div[@role='button'][.//span[contains(text(), 'Napisz coś do')]]",
            "//div[@role='textbox'][contains(@aria-placeholder, 'Napisz coś do')]",
            "//span[contains(text(), 'Napisz coś do')]/ancestor::div[@role='button'][1]",
            "//span[contains(text(), 'Write something to')]/ancestor::div[@role='button'][1]",
        ]
        opened = False
        for sel in composer_entry_selectors:
            try:
                el = WebDriverWait(driver, 5).until(
                    EC.element_to_be_clickable((By.XPATH, sel))
                )
                el.click()
                logger.info(f"✅ Opened composer via: {sel}")
                opened = True
                human_delay(2, 3)
                break
            except Exception:
                continue
        if not opened:
            logger.error("❌ Could not find/click 'Napisz coś do...' entry on profile wall")
            driver.save_screenshot(str(PROJECT_ROOT / "debug" / f"debug_wallpost_{safe_name}_error_no_entry.png"))
            return False

        # --- Step 2: Find the composer textbox (now in dialog) and paste URL.
        textbox_selectors = [
            "//div[@role='dialog']//div[@role='textbox'][@contenteditable='true']",
            "//div[@role='dialog']//div[@contenteditable='true']",
        ]
        textbox = None
        for sel in textbox_selectors:
            try:
                textbox = WebDriverWait(driver, 5).until(
                    EC.element_to_be_clickable((By.XPATH, sel))
                )
                break
            except Exception:
                continue
        if not textbox:
            logger.error("❌ No textbox found in profile-wall composer dialog")
            driver.save_screenshot(str(PROJECT_ROOT / "debug" / f"debug_wallpost_{safe_name}_error_no_textbox.png"))
            return False

        textbox.click()
        human_delay(0.5, 1)
        textbox.send_keys(our_post_url)
        logger.info(f"⌨️ Typed alert URL into composer")
        # FB needs a few seconds to fetch the URL and render the embed preview
        human_delay(4, 6)
        driver.save_screenshot(str(PROJECT_ROOT / "debug" / f"debug_wallpost_{safe_name}_02_url_entered.png"))

        # --- Step 3: Click Opublikuj (mirror of share-to-group fix ordering).
        publish_selectors = [
            "//div[@role='dialog']//div[@role='button']//span[text()='Opublikuj']",
            "//div[@role='dialog']//span[text()='Opublikuj']",
            "//div[@aria-label='Opublikuj' and @role='button']",
            "//div[@role='dialog']//span[text()='Post']",
        ]
        published = False
        for sel in publish_selectors:
            try:
                btn = WebDriverWait(driver, 5).until(
                    EC.element_to_be_clickable((By.XPATH, sel))
                )
                btn.click()
                logger.info(f"✅ Clicked Opublikuj: {sel}")
                published = True
                human_delay(3, 4)
                break
            except Exception:
                continue
        if not published:
            logger.error("❌ No publish button found in profile-wall composer")
            driver.save_screenshot(str(PROJECT_ROOT / "debug" / f"debug_wallpost_{safe_name}_error_no_publish.png"))
            return False

        # Wait for dialog to disappear (= publish confirmed by FB).
        try:
            WebDriverWait(driver, 15).until(
                EC.invisibility_of_element_located(
                    (By.XPATH, "//div[@role='dialog']//div[@role='textbox'][@contenteditable='true']")
                )
            )
            driver.save_screenshot(str(PROJECT_ROOT / "debug" / f"debug_wallpost_{safe_name}_03_after_publish.png"))
            logger.info(f"✅ Alert posted on profile wall: {profile_url}")
            return True
        except TimeoutException:
            logger.warning(f"⚠️ Dialog still open 15s after publish — uncertain success. "
                           f"Check {profile_url} manually.")
            driver.save_screenshot(str(PROJECT_ROOT / "debug" / f"debug_wallpost_{safe_name}_error_dialog_still_open.png"))
            return False
    except Exception as e:
        logger.error(f"❌ post_alert_to_profile_wall crashed: {e}")
        import traceback; traceback.print_exc()
        return False


def share_to_all_groups(driver, post_url: str, caption: str,
                         max_groups: int = None) -> int:
    """Share post to all configured Facebook groups.

    Switches to personal profile first (group sharing must be done as personal account),
    then iterates through SHARE_TO_GROUPS with delays between each share.

    Args:
        driver: Selenium WebDriver instance
        post_url: URL of the post to share. MUST be a specific post permalink —
                  never FB_PAGE_URL (the page profile). See wch 2026-06-20
                  incident: when post_url was FB_PAGE_URL, the page-admin
                  view's first "Udostępnij" attached to a TVWALBRZYCH 'Riese'
                  article instead of our just-published map.
        caption: Caption text for shared posts
        max_groups: Optional override for MAX_GROUPS_PER_RUN. Used in RCB
                    alert mode to push the post to a larger audience.

    Returns:
        Number of successful shares (0 to len(SHARE_TO_GROUPS)).
    """
    if not SHARE_TO_GROUPS_ENABLED:
        logger.info("ℹ️ Group sharing is disabled (SHARE_TO_GROUPS_ENABLED=False)")
        return 0

    if not SHARE_TO_GROUPS:
        logger.info("ℹ️ No groups configured for sharing (SHARE_TO_GROUPS is empty)")
        return 0

    # Guard: post_url MUST be a specific permalink. See docstring rationale.
    if not post_url:
        logger.error("❌ share_to_all_groups: post_url is empty — refusing to share. "
                     "Caller must pass the specific post permalink from "
                     "post_to_facebook_selenium return tuple.")
        return 0
    if post_url.rstrip('/') == FB_PAGE_URL.rstrip('/'):
        logger.error(f"❌ share_to_all_groups: post_url equals FB_PAGE_URL ({post_url}) — "
                     "this is the page profile, not a specific post. Refusing "
                     "to share (this is the wch 2026-06-20 bug).")
        return 0

    groups = list(SHARE_TO_GROUPS)

    # Apply max groups per run limit
    effective_max = max_groups if max_groups is not None else MAX_GROUPS_PER_RUN
    if max_groups is not None and max_groups != MAX_GROUPS_PER_RUN:
        logger.info(f"📣 max_groups override active: {max_groups} (default {MAX_GROUPS_PER_RUN})")
    if effective_max > 0 and len(groups) > effective_max:
        logger.info(f"📋 Limiting to {effective_max}/{len(groups)} groups this run")
        groups = groups[:effective_max]

    logger.info("=" * 60)
    logger.info(f"📤 STARTING GROUP SHARING: {len(groups)} groups")
    for i, name in enumerate(groups):
        logger.info(f"  {i+1}. {name}")
    logger.info(f"📍 Post URL: {post_url}")
    logger.info(f"⏱️ Delay between shares: {SHARE_DELAY_MIN}-{SHARE_DELAY_MAX}s (randomized)")
    logger.info("=" * 60)

    # First switch to personal profile (required for group sharing)
    logger.info("🔄 Step 1: Switching to personal profile...")
    if not switch_to_personal_profile(driver):
        logger.error("❌ Could not switch to personal profile, aborting all group shares")
        logger.error("❌ This means we're still logged in as the page, which cannot share to groups")
        return 0

    successful_shares = 0
    failed_groups = []
    skipped_groups = []
    consecutive_failures = 0
    CONSECUTIVE_FAIL_LIMIT = 3

    for i, group_name in enumerate(groups):
        logger.info(f"--- Group {i+1}/{len(groups)}: {group_name} ---")

        if share_post_to_group(driver, post_url, group_name, caption):
            successful_shares += 1
            consecutive_failures = 0
            logger.info(f"✅ [{i+1}/{len(groups)}] Shared to: {group_name}")
        else:
            failed_groups.append(group_name)
            consecutive_failures += 1
            logger.error(f"❌ [{i+1}/{len(groups)}] Failed to share to: {group_name}")
            if consecutive_failures >= CONSECUTIVE_FAIL_LIMIT:
                remaining = len(groups) - i - 1
                skipped_groups = groups[i+1:]
                logger.error(f"🚫 RATE LIMIT: {CONSECUTIVE_FAIL_LIMIT} consecutive failures — stopping. {remaining} groups skipped.")
                break

        # Delay between shares (except for last one)
        if i < len(groups) - 1:
            delay = random.uniform(SHARE_DELAY_MIN, SHARE_DELAY_MAX)
            logger.info(f"⏳ Waiting {delay:.0f}s before next group share...")
            time.sleep(delay)

    logger.info("=" * 60)
    logger.info(f"📊 GROUP SHARING SUMMARY:")
    logger.info(f"  Total groups: {len(groups)}")
    logger.info(f"  Successful: {successful_shares}")
    logger.info(f"  Failed: {len(failed_groups)}")
    if failed_groups:
        logger.info(f"  Failed groups: {failed_groups}")
    if skipped_groups:
        logger.info(f"  Skipped (rate limit): {len(skipped_groups)} — {skipped_groups}")
    logger.info("=" * 60)

    return successful_shares


# ============================================
# RCB ALERT DEDUPE STATE (mirror of wch)
# ============================================
RCB_ALERT_STATE_FILE = PROJECT_ROOT / "data" / "posted_alerts.json"


def _load_posted_alerts() -> dict:
    if not RCB_ALERT_STATE_FILE.exists():
        return {}
    try:
        import json
        return json.loads(RCB_ALERT_STATE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"⚠️ Could not load {RCB_ALERT_STATE_FILE}: {e} — starting fresh")
        return {}


def _save_posted_alerts(state: dict) -> None:
    import json
    RCB_ALERT_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    RCB_ALERT_STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _prune_posted_alerts(state: dict, grace_hours: int = 48) -> dict:
    now = datetime.now()
    pruned = {}
    for wid, data in state.items():
        try:
            d_to = datetime.strptime(data.get('obowiazuje_do', ''),
                                      "%Y-%m-%d %H:%M:%S")
            age_hours = (now - d_to).total_seconds() / 3600
            if age_hours <= grace_hours:
                pruned[wid] = data
        except Exception:
            pruned[wid] = data
    return pruned


def get_new_imgw_alerts() -> tuple:
    """Returns (new_warnings, all_active_warnings). See wch counterpart."""
    all_active = fetch_imgw_warnings(IMGW_TERYT_CODES)
    if not all_active:
        return ([], [])
    state = _load_posted_alerts()
    new = [w for w in all_active if w.get('id') not in state]
    return (new, all_active)


def _mark_alerts_posted(warnings: list) -> None:
    state = _load_posted_alerts()
    state = _prune_posted_alerts(state)
    now_iso = datetime.now().isoformat()
    for w in warnings:
        wid = w.get('id')
        if not wid:
            continue
        state[wid] = {
            'nazwa_zdarzenia': w.get('nazwa_zdarzenia'),
            'stopien': w.get('stopien'),
            'prawdopodobienstwo': w.get('prawdopodobienstwo'),
            'obowiazuje_od': w.get('obowiazuje_od'),
            'obowiazuje_do': w.get('obowiazuje_do'),
            'posted_at': now_iso,
        }
    _save_posted_alerts(state)


# ============================================
# RCB ALERT — MAP COMPOSITION + PUBLISH
# ============================================

# Event-type → "is storm-themed" classifier (mirrors wch). Storm cloud
# overlay only justified for these events. Overlaying it on a HEAT alert
# (Upał) is visually wrong (user feedback 2026-06-26). Other event types
# fall back to a sunny/cloudy base depending on time of day, without
# overlay, until dedicated event-type icons exist.
RCB_STORM_THEMED_EVENTS = {"Burze", "Burze z gradem", "Trąby powietrzne"}


def _compose_bg_alert_map(districts_data: list, warnings: list) -> tuple:
    """Build the bg alert map image from scratch.

    Visual decision based on PRIMARY warning event type (warnings[0]):
      - Storm-themed event (Burze etc.): map_storm.png + storm cloud overlay
      - Other (Upał, Mróz, Silny wiatr…): map_sun.png or map_moon.png based
        on time of day, NO storm overlay
    Temperatures always in Scarlet (alert visual signal). Banner + cards
    are added separately by compose_alert_top().

    Returns (path, min_t, max_t).
    """
    output_path = PROJECT_ROOT / "output" / OUTPUT_IMAGE_FILENAME
    output_path.parent.mkdir(parents=True, exist_ok=True)

    primary_event = (warnings[0].get('nazwa_zdarzenia', '') if warnings else '')
    use_storm_visual = primary_event in RCB_STORM_THEMED_EVENTS
    is_night = get_forecast_mode() == 'night'

    if use_storm_visual:
        base_filename = RCB_ALERT_STORM_BASE  # map_storm.png
        logger.info(f"🚨 RCB ALERT MAP (Burze-themed): {base_filename} + storm overlay")
    else:
        base_filename = "map_moon.png" if is_night else "map_sun.png"
        logger.info(f"🚨 RCB ALERT MAP ({primary_event!r}): {base_filename} (no overlay)")

    base_path = MAPS_DIR / base_filename
    if not base_path.exists():
        logger.warning(f"⚠️ {base_path} missing — falling back to map_cloud.png")
        base_path = MAPS_DIR / "map_cloud.png"

    base = Image.open(base_path).convert('RGBA')

    # Storm overlay ONLY for storm-themed events
    if use_storm_visual:
        overlay_path = MAPS_DIR / RCB_ALERT_STORM_OVERLAY
        if overlay_path.exists():
            overlay = Image.open(overlay_path).convert('RGBA')
            oh = int(overlay.size[1] * (RCB_OVERLAY_W / overlay.size[0]))
            overlay = overlay.resize((RCB_OVERLAY_W, oh), Image.LANCZOS)
            base.alpha_composite(overlay, dest=(RCB_OVERLAY_X, RCB_OVERLAY_Y))
            logger.info(f"✅ Storm overlay {RCB_OVERLAY_W}×{oh} composited at "
                        f"({RCB_OVERLAY_X}, {RCB_OVERLAY_Y})")
        else:
            logger.warning(f"⚠️ Storm overlay {overlay_path} missing — base only")

    draw = ImageDraw.Draw(base)
    font_temp = get_font(55, bold=True)
    font_info = get_font(24, bold=True)

    min_t, max_t = 100, -100
    for d in districts_data:
        temp = round(d['temp'])
        if temp < min_t: min_t = temp
        if temp > max_t: max_t = temp
        temp_str = f"{temp:+d}°" if temp != 0 else "0°"
        draw_text_centered(draw, d['x'], d['y'], temp_str, font_temp,
                           RCB_ALERT_TEMP_COLOR)

    now_str = datetime.now().strftime("%d.%m.%Y godz. %H:%M")
    draw.text((base.size[0] - 450, base.size[1] - 25),
              f"Stan na: {now_str} | Dane: Open-Meteo + IMGW",
              font=font_info, fill=(0, 0, 0))

    base = add_charity_overlay(base)
    base.save(output_path, "PNG")
    logger.info(f"✅ Alert base map saved: {output_path} (temps {min_t}-{max_t}°C)")
    return str(output_path), min_t, max_t


def compose_alert_top(map_path: str, warnings: list) -> str:
    """Prepend broadcast banner (banerTopRCB.png) + dynamic warning cards
    + drop shadow on top of the alert map. Mutates file in place.
    """
    if not warnings:
        return map_path
    try:
        base = Image.open(map_path).convert('RGBA')
        map_w, map_h = base.size

        banner_path = MAPS_DIR / RCB_ALERT_BANNER_FILE
        if not banner_path.exists():
            logger.warning(f"⚠️ {banner_path} missing — skipping alert banner overlay")
            return map_path
        banner_orig = Image.open(banner_path).convert('RGBA')
        bw, bh = banner_orig.size
        top_h = int(bh * (map_w / bw))
        banner = banner_orig.resize((map_w, top_h), Image.LANCZOS)

        banner_block_h = top_h + RCB_CARDS_HEIGHT
        canvas = Image.new('RGBA', (map_w, map_h + banner_block_h),
                           RCB_CARD_BG + (255,))
        canvas.paste(banner, (0, 0))
        d = ImageDraw.Draw(canvas)
        d.rectangle([0, top_h, map_w, banner_block_h], fill=RCB_CARD_BG)

        font_card_title = get_font(22, bold=True)
        font_card_time = get_font(20, bold=True)

        def _accent(st_str):
            return RCB_CARD_ACCENT_RED if st_str in ('2', '3') else RCB_CARD_ACCENT_YELLOW

        def _card(x, y, w, h, accent, name, stopien, time_range):
            d.rectangle([x, y, x + 8, y + h], fill=accent)
            ix = x + 8 + 18
            title = f"{name.upper()}  —  STOPIEŃ {stopien}"
            d.text((ix, y + 14), title, font=font_card_title, fill=RCB_CARD_INK)
            d.text((ix, y + 52), time_range, font=font_card_time, fill=RCB_CARD_INK_SOFT)

        pad = 8
        card_w = (map_w - 3 * pad) // 2

        def _card_for(idx, x_off):
            wd = warnings[idx]
            df = wd.get('obowiazuje_od', '') or ''
            dt = wd.get('obowiazuje_do', '') or ''
            tr = (f"{df[8:10]}.{df[5:7]} {df[11:16]} → "
                  f"{dt[8:10]}.{dt[5:7]} {dt[11:16]}") if df and dt else ""
            _card(x_off, top_h + 8, card_w, RCB_CARDS_HEIGHT - 16,
                  _accent(str(wd.get('stopien'))),
                  wd.get('nazwa_zdarzenia', '?'),
                  str(wd.get('stopien', '?')), tr)

        if len(warnings) >= 1:
            _card_for(0, pad)
        if len(warnings) >= 2:
            _card_for(1, 2 * pad + card_w)
            sep_x = 2 * pad + card_w - pad // 2
            d.line([(sep_x, top_h + 18), (sep_x, banner_block_h - 18)],
                   fill=RCB_CARD_SEPARATOR, width=1)

        canvas.paste(base, (0, banner_block_h))

        # Drop shadow under banner block
        sh = Image.new('RGBA', (map_w, RCB_SHADOW_HEIGHT), (0, 0, 0, 0))
        sd = ImageDraw.Draw(sh)
        for i in range(RCB_SHADOW_HEIGHT):
            t = i / max(RCB_SHADOW_HEIGHT - 1, 1)
            a = int(RCB_SHADOW_MAX_ALPHA * (1 - t) ** 1.6)
            sd.line([(0, i), (map_w, i)], fill=(0, 0, 0, a))
        canvas.alpha_composite(sh, dest=(0, banner_block_h))

        canvas.convert('RGB').save(map_path, "PNG")
        logger.info(f"✅ Alert banner composed onto {map_path} "
                    f"(banner_h={top_h}, cards_h={RCB_CARDS_HEIGHT})")
        return map_path
    except Exception as e:
        logger.error(f"❌ compose_alert_top failed: {e}")
        return map_path


def publish_rcb_alert_only(warnings: list) -> bool:
    """Full bg alert publish flow — generates alert map (with storm overlay
    + scarlet temps + banner + cards), posts to bg FB page, shares to all
    configured groups, and posts to each RCB_ALERT_EXTRA_PROFILE_POSTS
    wall (e.g. Straż Miejska).

    Called by hourly bg_rcb_alert_checker.py only when a NEW warning ID
    is detected (dedupes via posted_alerts.json).
    """
    if not warnings:
        logger.warning("publish_rcb_alert_only called with empty warnings — no-op")
        return False

    logger.info("=" * 60)
    logger.info(f"🚨 RCB ALERT publish (bg) — {len(warnings)} active warning(s)")
    for w in warnings:
        logger.info(f"   • {w.get('nazwa_zdarzenia')} st={w.get('stopien')} "
                    f"({w.get('obowiazuje_od')} → {w.get('obowiazuje_do')})")
    logger.info("=" * 60)

    # 1. Weather data
    districts_weather = fetch_districts_weather()
    if not districts_weather:
        logger.error("Brak danych pogodowych — przerywam alert publish.")
        return False

    forecast = fetch_forecast_center()
    mode = forecast.get('forecast_mode', 'day') if forecast else 'day'
    forecast_desc = None
    try:
        if forecast and forecast.get('hourly') and forecast['hourly'].get('temps'):
            forecast_text, forecast_desc = generate_professional_forecast_text(
                forecast['hourly'], mode)
        elif forecast:
            forecast_text = generate_forecast_text(forecast)
        else:
            forecast_text = "Sprawdź temperaturę w swojej dzielnicy na mapie."
    except Exception as e:
        logger.error(f"Forecast generation error: {e}")
        forecast_text = "Sprawdź temperaturę w swojej dzielnicy na mapie."

    # 2. Build alert map (storm base + overlay + scarlet temps)
    map_path, min_t, max_t = _compose_bg_alert_map(districts_weather, warnings)

    # 3. Compose banner on top
    compose_alert_top(map_path, warnings)

    # 4. Caption
    desc = forecast_desc or "Pochmurno"
    range_str = format_temp(min_t) if min_t == max_t else f"od {format_temp(min_t)} do {format_temp(max_t)}"

    warnings_block = format_warnings_for_caption(warnings)
    warnings_prefix = (warnings_block + "\n\n") if warnings_block else ""

    caption = f"""{warnings_prefix}🌡️ Aktualna temperatura w Boguszowie-Gorcach: {range_str}. {desc}.
{forecast_text}

❤️ Mieszkańcu Boguszowa-Gorc — możesz wesprzeć lokalną fundację.
👉 To nic Cię nie kosztuje. KRS: 0000498479

Więcej: {FB_PROFILE_LINK}

#BoguszówGorce #Boguszów #DolnyŚląsk"""

    # 5. Verify needle — bg uses ", prawdopodobieństwo" format
    w0 = warnings[0]
    _name = w0.get('nazwa_zdarzenia', '?')
    _st = w0.get('stopien', '?')
    _prob = w0.get('prawdopodobienstwo', '')
    if _prob:
        verify_needle = f"{_name} — stopień {_st}, prawdopodobieństwo {_prob}%"
    else:
        verify_needle = f"{_name} — stopień {_st}"

    # 6. Driver + login + post
    driver = None
    try:
        driver = setup_chrome_driver_with_retry()
        if not ensure_logged_in_as_page(driver):
            logger.error("Could not verify page login — aborting alert publish.")
            return False

        success, page_post_url = post_to_facebook_selenium(
            driver, map_path, caption,
            verify_needle=verify_needle, test_mode=False,
        )
        if not success or not page_post_url:
            logger.error("Alert page post failed verification — NOT updating state.")
            return False

        logger.info(f"✅ Alert published + verified: {page_post_url}")

        # 7. Share to ALL groups (alert mode = full group list)
        if SHARE_TO_GROUPS_ENABLED and SHARE_TO_GROUPS:
            share_cap = len(SHARE_TO_GROUPS)
            shares_count = share_to_all_groups(
                driver, page_post_url, caption, max_groups=share_cap)
            logger.info(f"📊 Shared to {shares_count} groups")

        # 8. RCB-only profile-wall distribution (Straż Miejska etc.)
        if RCB_ALERT_EXTRA_PROFILE_POSTS:
            wall_ok = 0
            for prof_url in RCB_ALERT_EXTRA_PROFILE_POSTS:
                try:
                    if post_alert_to_profile_wall(driver, prof_url, page_post_url,
                                                   is_alert=True):
                        wall_ok += 1
                except Exception as e:
                    logger.error(f"Profile wall post to {prof_url} failed: {e}")
            logger.info(f"📤 Profile walls: {wall_ok}/"
                        f"{len(RCB_ALERT_EXTRA_PROFILE_POSTS)} succeeded")

        # 9. Mark warnings as posted
        _mark_alerts_posted(warnings)
        return True
    except Exception as e:
        logger.error(f"❌ publish_rcb_alert_only crashed: {e}")
        import traceback; traceback.print_exc()
        return False
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


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

        forecast_desc = None
        try:
            if forecast and forecast.get('hourly') and forecast['hourly'].get('temps'):
                # Use professional forecast
                forecast_text, forecast_desc = generate_professional_forecast_text(forecast['hourly'], mode)
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
        # Use forecast-based description if available, otherwise fall back to current weather code
        if forecast_desc:
            desc = forecast_desc
        else:
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

                        # Use page URL directly for group sharing — the most recent post
                        # in the feed will have the Share button. Resolving a specific post URL
                        # is unreliable (picks up foreign page posts or broken permalinks).
                        post_url = FB_PAGE_URL
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
