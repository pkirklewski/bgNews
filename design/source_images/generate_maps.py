#!/usr/bin/env python3
"""
Generate weather condition maps for Boguszów-Gorce.

Takes the base map.png and composites each weather icon on top
in the top-right corner with a drop shadow effect.

Source icons are borrowed from the Wałbrzych project:
  /home/pkirklewski/scripts/wchNews/design/source_images/

Output maps go to:
  /home/pkirklewski/scripts/bgnews/assets/weather_maps/

Usage:
    python generate_maps.py
"""

from pathlib import Path
from PIL import Image, ImageFilter

# Paths
SCRIPT_DIR = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
BASE_MAP = SCRIPT_DIR / "map.png"
ICON_SOURCE_DIR = Path("/home/pkirklewski/scripts/wchNews/design/source_images")
OUTPUT_DIR = PROJECT_ROOT / "assets" / "weather_maps"

# Icon placement config
# Base map is 1227x1416. Icon goes in top-right area.
# We scale the 1024x1024 icons down to this size:
ICON_SIZE = 600
# Position: upper-right corner, partially overhanging edges
ICON_PADDING_RIGHT = -50   # was 40, moved 90px to the right
ICON_PADDING_TOP = -85     # was 15, moved 100px up

# Drop shadow config - mimics the compass rose (róża wiatrów) shadow style:
# wide, dispersed, softly spread halo rather than a tight directional shadow
SHADOW_OFFSET = 10
SHADOW_COLOR = (160, 160, 160, 60)  # Light grey with moderate opacity for a gentle halo
SHADOW_BLUR_RADIUS = 12

# Map definitions: output_name -> icon_filename
MAPS = {
    "map_sun.png": "sun.png",
    "map_cloud_sun.png": "cloud_sun.png",
    "map_cloud.png": "cloud.png",
    "map_fog.png": "fog.png",
    "map_rain_light.png": "rain_light.png",
    "map_rain.png": "rain.png",
    "map_rain_snow.png": "rain_snow.png",
    "map_snow_light.png": "snow_light.png",
    "map_snow.png": "snow.png",
    "map_storm.png": "storm.png",
}


def create_drop_shadow(icon: Image.Image) -> Image.Image:
    """
    Create a dispersed drop shadow mimicking the compass rose style.

    Uses an expanded canvas so the Gaussian blur can spread outward
    without being clipped at the edges, producing a soft ambient halo.
    """
    # Expand canvas by blur margin on all sides so blur isn't clipped
    margin = SHADOW_BLUR_RADIUS * 3
    w, h = icon.size
    expanded_size = (w + 2 * margin, h + 2 * margin)

    # Build shadow using the icon's alpha channel as a mask
    # Create a solid shadow-colored layer at icon size
    solid = Image.new('RGBA', (w, h), SHADOW_COLOR)
    # Apply the icon's alpha as mask so only the icon silhouette remains
    alpha = icon.split()[3]
    solid.putalpha(alpha)

    # Place into expanded canvas
    shadow = Image.new('RGBA', expanded_size, (0, 0, 0, 0))
    shadow.paste(solid, (margin, margin), solid)

    # Apply wide Gaussian blur for the dispersed look
    shadow = shadow.filter(ImageFilter.GaussianBlur(radius=SHADOW_BLUR_RADIUS))

    return shadow, margin


def generate_map(base_map: Image.Image, icon_path: Path, output_path: Path):
    """Composite a weather icon onto the base map with dispersed drop shadow."""
    # Copy the base map
    result = base_map.copy()

    # Load and resize the icon
    icon = Image.open(icon_path).convert('RGBA')
    icon = icon.resize((ICON_SIZE, ICON_SIZE), Image.LANCZOS)

    # Calculate position (top-right area)
    x = result.width - ICON_SIZE - ICON_PADDING_RIGHT
    y = ICON_PADDING_TOP

    # Create and paste drop shadow first
    # Shadow canvas is larger than icon (has margin for blur spread)
    shadow, margin = create_drop_shadow(icon)
    shadow_pos = (x - margin + SHADOW_OFFSET, y - margin + SHADOW_OFFSET)
    result.paste(shadow, shadow_pos, shadow)

    # Paste the icon on top
    result.paste(icon, (x, y), icon)

    # Save
    result.save(output_path, 'PNG')
    print(f"  Created: {output_path.name} ({result.size[0]}x{result.size[1]})")


def main():
    # Verify base map exists
    if not BASE_MAP.exists():
        print(f"ERROR: Base map not found: {BASE_MAP}")
        return

    # Ensure output directory exists
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Load base map once
    print(f"Loading base map: {BASE_MAP} ...")
    base_map = Image.open(BASE_MAP).convert('RGBA')
    print(f"  Size: {base_map.size[0]}x{base_map.size[1]}")
    print(f"  Icon size: {ICON_SIZE}x{ICON_SIZE}")
    print(f"  Icon position: top-right ({base_map.width - ICON_SIZE - ICON_PADDING_RIGHT}, {ICON_PADDING_TOP})")
    print()

    # Generate each map
    success = 0
    for output_name, icon_name in MAPS.items():
        icon_path = ICON_SOURCE_DIR / icon_name
        if not icon_path.exists():
            print(f"  WARNING: Icon not found: {icon_path}")
            continue

        output_path = OUTPUT_DIR / output_name
        generate_map(base_map, icon_path, output_path)
        success += 1

    print(f"\nDone! Generated {success}/{len(MAPS)} weather maps.")


if __name__ == "__main__":
    main()
