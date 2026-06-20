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

After posting the weather map, the script automatically shares it to a Facebook group
(as personal profile "Piotr Kirklewski"):
- BOGUSZÓW-GORCE/Ogłoszenia/Informacje/Sprzedam/Kupię/Zamienię/

(Earlier configurations included "Ogłoszenia Boguszów-Gorce" and "Społeczność Kuźnic"
but those were rejecting our posts — `SHARE_TO_GROUPS` was pruned to the one
group that accepts daily content.)

Group sharing switches from the page profile to personal profile, navigates to the
post, clicks Share > Share to group, searches for the group, enters a caption,
and publishes. Post URL detection filters for the page's own posts (kangurello /
100027689516729) to avoid sharing foreign posts from the feed. All steps are logged
with debug screenshots.

## IMGW Meteo Alert Mode (RCB-style)

Mirror of the wch alert architecture (see wchNews README for the visual /
banner / verification design). bg uses TERYT **`0221`** (powiat wałbrzyski,
where Boguszów-Gorce sits — verified via Nominatim reverse-geocode on all
7 DISTRICTS).

Status:
- **Safety machinery: in place** — `_derive_verification_needle`,
  `verify_post_published_and_get_url`, tuple-returning
  `post_to_facebook_selenium`, `FB_PAGE_URL` guard in
  `share_to_all_groups`, pre-publish embed guard in `share_post_to_group`.
  All identical to wch (commit `6a7dce7`).
- **Alert-mode wiring in `main()`: pending** — `generate_map_image()`
  is not yet alert-aware. Today's bg alert post was produced via the
  one-off `/tmp/bg_publish_alert_test.py` against a hand-positioned
  preview (`/tmp/bg_map_preview_v5.png`: `map_storm.png` + overlay
  `stormRCB2transpartentBCKG.png` at `OFF_X=680, OFF_Y=-90` size 640×640,
  plus the broadcast banner + cards + Scarlet temps composed on top).
  Wiring this into `main()` so the regular cron handles alerts
  automatically is a remaining task.

### RCB-only extra distribution (`RCB_ALERT_EXTRA_PROFILE_POSTS`)

Some local-service profiles (e.g. **Straż Miejska Boguszów-Gorce**)
have agreed to receive RCB-class meteo alerts pasted onto their
profile wall via FB's "Napisz coś do <X>..." composer. FB auto-renders
the pasted post URL as a rich embed; no moderator approval needed
(unlike groups).

**⚠️ Ban-risk discipline**: these profiles must **NEVER** receive
regular daily city-news posts — only alerts. Three independent safety
layers enforce this:

1. `RCB_ALERT_EXTRA_PROFILE_POSTS` is a **separate config list** —
   never merged into `SHARE_TO_GROUPS`. The regular daily share path
   (`share_to_all_groups`) cannot reach these profiles by construction.
2. `post_alert_to_profile_wall(driver, profile_url, our_post_url, is_alert: bool)`
   **requires `is_alert=True`** and raises `ValueError` if False —
   accidental invocation from a non-alert context is a hard, loud
   crash, not a silent ban.
3. Big warning docstrings on both the config block and the function
   spell out the architecture for future contributors.

Currently configured recipients:
| Profile | FB ID | Notes |
|---|---|---|
| Straż Miejska Boguszów-Gorce | `100065918171599` | Accepts RCB-class meteo alerts (storms, heat, frost) |

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

- **2026-06-20** — Add IMGW alert helpers + bug-proof publish/share verification (mirror of wch `ac71b0a`). Adds `_derive_verification_needle`, `verify_post_published_and_get_url`, tuple-returning `post_to_facebook_selenium` with strict caption-needle DOM verification, FB_PAGE_URL guard in `share_to_all_groups`, pre-publish embed guard in `share_post_to_group`. Dismisses "Organizujesz wydarzenie?" upsell. Adds asset files `banerTopRCB.png` + `stormRCB2transpartentBCKG.png` for storm-icon overlay over existing `map_storm.png`. **Alert-mode wiring in `main()` still pending.**
- **2026-06-20** — Add `WMAP_FAST_MODE` env var for ad-hoc/emergency runs (shortens `human_delay` to 0.3 s; keeps anti-spam `SHARE_DELAY_MIN/MAX` at production values).
- **2026-06-20** — Add `RCB_ALERT_EXTRA_PROFILE_POSTS` config + `post_alert_to_profile_wall()` for distributing alerts to local-service profiles (Straż Miejska Boguszów-Gorce). Triple-guarded ALERT-ONLY (separate list + `is_alert=True` runtime guard + warning docs) — these profiles must never receive regular daily content (ban risk).
- **2026-06-20** — Fix `share_post_to_group` publish-click selector ordering: lead with `Opublikuj` inside button role instead of brittle `Udostępnij` inside obfuscated `x1qjc9v5` class which matched a secondary share-count link, not the real submit button.
- **2026-02-10** — Update post caption template to match unified format (cleaner forecast text, new charity block)
- **2026-02-10** — Add night mode maps (moon, cloud_moon, fog_moon) — automatically used for evening runs
