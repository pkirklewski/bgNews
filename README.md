# Boguszów-Gorce Newsy i Informacje

Automated news aggregation and weather reporting for the Facebook page
**Boguszów-Gorce Newsy i Informacje** (ID: 100027689516729).

## Architecture

Three automation pillars run on a schedule via cron:

| Script | Purpose | Schedule |
|--------|---------|----------|
| `src/bg_weather_map_selenium.py` | Weather map with temps for 7 districts + share to 3 groups | 06:30, 19:15 |
| `src/bg_scraper_selenium.py` | Web scraper (3 sources, "Bogusz" filter) | 07:00, 11:00, 15:00, 19:00 |
| `src/bg_fb_share.py` | Auto-share posts from 4 monitored FB pages | 07:30, 11:30, 15:30, 19:30 |

All Facebook interaction runs through a Selenium Chrome instance in Docker.

## Project Structure

```
bgnews/
├── src/                          # Source code
│   ├── bg_weather_map_selenium.py  # Weather map generator
│   ├── bg_scraper_selenium.py      # Web article scraper
│   ├── bg_fb_share.py              # Facebook share bot
│   ├── docker_selenium.py          # Docker Selenium connection helper
│   ├── docker_fb_login.py          # Interactive FB login setup
│   └── chrome_profile_manager.py   # Chrome profile health manager
├── assets/weather_maps/          # Pre-composed condition maps (13) + overlay
├── design/source_images/         # Base map + map generation script
├── data/                         # Runtime data (sent/shared posts JSON)
├── docker-data/                  # Chrome profile (gitignored)
├── output/                       # Generated images (gitignored)
├── debug/                        # Debug screenshots (gitignored)
├── logs/                         # Log files (gitignored)
├── locks/                        # File locks for concurrency
├── docker-compose.yml            # Selenium Chrome container
└── requirements.txt              # Python dependencies
```

## Weather Maps

13 condition-based maps generated from a base map + weather icons:
sun, moon, cloud_sun, cloud_moon, cloud, fog, fog_moon, rain_light, rain, rain_snow, snow_light, snow, storm.
Night variants (moon, cloud_moon, fog_moon) are used automatically for evening runs.

Temperature data from [Open-Meteo API](https://open-meteo.com/) for 7 districts:
Lubominek, Chelmiec, Gorce, Boguszow-Gorce, Stary Lesieniec, Kuznice Swidnickie, Dzikowiec.

Regenerate maps: `python design/source_images/generate_maps.py`

## Group Sharing (Weather Map)

After posting the weather map, the script automatically shares it to 3 Facebook groups
(as personal profile "Piotr Kirklewski"):
- BOGUSZÓW-GORCE
- Ogłoszenia Boguszów-Gorce
- Społeczność Kuźnic

Group sharing switches from the page profile to personal profile, navigates to the
post, clicks Share > Share to group, searches for the group, enters a caption,
and publishes. Post URL detection filters for the page's own posts (kangurello /
100027689516729) to avoid sharing foreign posts from the feed. All steps are logged
with debug screenshots.

## Web Sources (Scraper)

Articles are scraped from 3 sources and filtered for "Bogusz" (case-insensitive):
- dziennik.walbrzych.pl
- walbrzych.policja.gov.pl
- tvwalbrzych.pl

Matching articles are posted to Facebook using the link preview method.

## Monitored Facebook Pages (Share Bot)

Posts from these pages are auto-shared to the feed:
- gminamiastoboguszowgorce (Municipal)
- GornikBoguszowGorce (Sports club)
- MBPCK (Library)
- ospboguszow (Fire department)

## Setup

### 1. Docker

```bash
docker compose up -d
```

Container: `bg-selenium-chrome` on ports 4445 (Selenium) / 7901 (noVNC).

### 2. Facebook Login

```bash
python src/docker_fb_login.py
```

Open http://localhost:7901 (password: `secret`), log into Facebook, and switch to the page profile.

### 3. Dependencies

```bash
pip install -r requirements.txt
playwright install chromium
```

### 4. Test

Each script has a `TEST_MODE` flag at the top. Set to `True` to run the full pipeline without publishing.

## Docker

| Port | Service |
|------|---------|
| 4445 | Selenium WebDriver |
| 7901 | noVNC (browser view, password: `secret`) |

Chrome profile persists in `docker-data/chrome-profile/` (volume mount).

## Changelog

- **2026-02-10** — Add night mode maps (moon, cloud_moon, fog_moon) — automatically used for evening runs
