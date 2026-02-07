#!/usr/bin/env python3
"""
Chrome Profile Health Manager for Boguszów-Gorce Selenium Automation
=====================================================================
Diagnoses, prevents, and recovers from Chrome profile corruption.

For Docker setup, the profile lives at: docker-data/chrome-profile/
For local setup, it's at: ~/.config/chrome-fb-bot-bg/

Usage:
    # As standalone diagnostic
    python3 src/chrome_profile_manager.py --check
    python3 src/chrome_profile_manager.py --fix
    python3 src/chrome_profile_manager.py --backup

    # From your scripts
    from chrome_profile_manager import ensure_healthy_profile, create_backup

    if not ensure_healthy_profile():
        logger.error("Could not recover profile - manual intervention needed")
        sys.exit(1)

    driver = setup_chrome_driver()  # Now safe to start
"""

import os
import sys
import shutil
import signal
import logging
import subprocess
import time
import atexit
from pathlib import Path
from datetime import datetime
from typing import Optional, Tuple

# ============================================
# CONFIGURATION
# ============================================

PROJECT_ROOT = Path(__file__).parent.parent
CHROME_PROFILE = PROJECT_ROOT / "docker-data" / "chrome-profile"
BACKUP_DIR = PROJECT_ROOT / "docker-data" / "chrome-profile-backups"
MAX_BACKUPS = 5  # Keep last N backups

# Files essential for login persistence (DO NOT DELETE these during cleanup)
ESSENTIAL_FILES = [
    "Cookies",
    "Cookies-journal",
    "Login Data",
    "Login Data-journal",
    "Web Data",
    "Web Data-journal",
    "Local State",
    "Preferences",
    "Secure Preferences",
    "Network/Cookies",
    "Network/Cookies-journal",
    "Default/Cookies",
    "Default/Cookies-journal",
    "Default/Login Data",
    "Default/Login Data-journal",
    "Default/Web Data",
    "Default/Preferences",
]

# Files that can cause lock issues (safe to delete)
LOCK_FILES = [
    "SingletonLock",
    "SingletonCookie",
    "SingletonSocket",
    "lockfile",
    "parent.lock",
]

# Directories that can be safely cleared to fix corruption
SAFE_TO_CLEAR = [
    "Cache",
    "Code Cache",
    "GPUCache",
    "ShaderCache",
    "GrShaderCache",
    "Service Worker/CacheStorage",
    "Service Worker/ScriptCache",
]

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ============================================
# DIAGNOSTIC FUNCTIONS
# ============================================

def check_singleton_locks() -> dict:
    """Check status of Singleton lock files."""
    status = {
        "has_locks": False,
        "locks": [],
        "stale_locks": [],
        "active_locks": []
    }

    for lock_name in LOCK_FILES:
        lock_path = CHROME_PROFILE / lock_name
        if lock_path.exists() or lock_path.is_symlink():
            status["has_locks"] = True
            status["locks"].append(lock_name)

            if lock_name == "SingletonLock" and lock_path.is_symlink():
                try:
                    target = os.readlink(lock_path)
                    if "-" in target:
                        pid_str = target.split("-")[-1]
                        try:
                            pid = int(pid_str)
                            os.kill(pid, 0)
                            status["active_locks"].append((lock_name, pid))
                        except (ValueError, ProcessLookupError):
                            status["stale_locks"].append(lock_name)
                        except PermissionError:
                            status["active_locks"].append((lock_name, pid))
                except Exception:
                    status["stale_locks"].append(lock_name)
            else:
                status["stale_locks"].append(lock_name)

    return status


def check_profile_health() -> dict:
    """Comprehensive health check of Chrome profile."""
    health = {
        "exists": CHROME_PROFILE.exists(),
        "healthy": True,
        "issues": [],
        "warnings": [],
        "lock_status": None,
        "size_mb": 0,
        "essential_files_present": [],
        "essential_files_missing": [],
    }

    if not health["exists"]:
        health["healthy"] = False
        health["issues"].append("Profile directory does not exist")
        return health

    # Check size
    try:
        total_size = sum(
            f.stat().st_size for f in CHROME_PROFILE.rglob('*') if f.is_file()
        )
        health["size_mb"] = round(total_size / (1024 * 1024), 2)
    except Exception as e:
        health["warnings"].append(f"Could not calculate profile size: {e}")

    # Check lock status
    health["lock_status"] = check_singleton_locks()

    if health["lock_status"]["stale_locks"]:
        health["healthy"] = False
        health["issues"].append(
            f"Stale lock files found: {health['lock_status']['stale_locks']}"
        )

    if health["lock_status"]["active_locks"]:
        health["warnings"].append(
            f"Chrome is currently running with this profile: {health['lock_status']['active_locks']}"
        )

    # Check essential files
    for essential in ESSENTIAL_FILES:
        essential_path = CHROME_PROFILE / essential
        if essential_path.exists():
            health["essential_files_present"].append(essential)
        else:
            if "Cookies" in essential and "journal" not in essential:
                health["essential_files_missing"].append(essential)

    # Check for common corruption indicators
    local_state = CHROME_PROFILE / "Local State"
    if local_state.exists():
        try:
            content = local_state.read_text()
            if len(content) < 100:
                health["warnings"].append("Local State file is suspiciously small")
        except Exception as e:
            health["issues"].append(f"Cannot read Local State: {e}")
            health["healthy"] = False

    # Check for crash indicators
    crash_dir = CHROME_PROFILE / "Crash Reports"
    if crash_dir.exists():
        recent_crashes = list(crash_dir.glob("*.dmp"))
        if len(recent_crashes) > 10:
            health["warnings"].append(f"Found {len(recent_crashes)} crash dumps")

    return health


# ============================================
# RECOVERY FUNCTIONS
# ============================================

def remove_stale_locks() -> bool:
    """Remove stale lock files."""
    removed = []
    for lock_name in LOCK_FILES:
        lock_path = CHROME_PROFILE / lock_name
        try:
            if lock_path.is_symlink():
                lock_path.unlink()
                removed.append(lock_name)
            elif lock_path.exists():
                lock_path.unlink()
                removed.append(lock_name)
        except Exception as e:
            logger.error(f"Failed to remove {lock_name}: {e}")

    if removed:
        logger.info(f"Removed stale locks: {removed}")

    return True


def clear_cache_dirs() -> bool:
    """Clear cache directories to free space and fix cache corruption."""
    cleared = []
    for cache_dir_name in SAFE_TO_CLEAR:
        cache_path = CHROME_PROFILE / cache_dir_name
        if cache_path.exists():
            try:
                shutil.rmtree(cache_path)
                cleared.append(cache_dir_name)
            except Exception as e:
                logger.warning(f"Could not clear {cache_dir_name}: {e}")

    if cleared:
        logger.info(f"Cleared cache directories: {cleared}")

    return True


def create_backup() -> Optional[Path]:
    """Create a backup of essential profile files."""
    if not CHROME_PROFILE.exists():
        logger.warning("Profile does not exist - nothing to backup")
        return None

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = BACKUP_DIR / f"backup_{timestamp}"
    backup_path.mkdir(parents=True, exist_ok=True)

    backed_up = []
    for essential in ESSENTIAL_FILES:
        src = CHROME_PROFILE / essential
        if src.exists():
            dst = backup_path / essential
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(src, dst)
                backed_up.append(essential)
            except Exception as e:
                logger.warning(f"Could not backup {essential}: {e}")

    if backed_up:
        logger.info(f"Backed up {len(backed_up)} files to {backup_path}")
        _cleanup_old_backups()
        return backup_path

    return None


def _cleanup_old_backups():
    """Remove old backups keeping only MAX_BACKUPS most recent."""
    if not BACKUP_DIR.exists():
        return

    backups = sorted(BACKUP_DIR.glob("backup_*"), reverse=True)

    for old_backup in backups[MAX_BACKUPS:]:
        try:
            shutil.rmtree(old_backup)
            logger.debug(f"Removed old backup: {old_backup}")
        except Exception as e:
            logger.warning(f"Could not remove old backup {old_backup}: {e}")


def restore_from_backup(backup_path: Optional[Path] = None) -> bool:
    """Restore essential files from backup."""
    if backup_path is None:
        if not BACKUP_DIR.exists():
            logger.error("No backup directory found")
            return False

        backups = sorted(BACKUP_DIR.glob("backup_*"), reverse=True)
        if not backups:
            logger.error("No backups found")
            return False

        backup_path = backups[0]

    if not backup_path.exists():
        logger.error(f"Backup path does not exist: {backup_path}")
        return False

    logger.info(f"Restoring from {backup_path}")

    CHROME_PROFILE.mkdir(parents=True, exist_ok=True)

    restored = []
    for item in backup_path.rglob("*"):
        if item.is_file():
            relative = item.relative_to(backup_path)
            dst = CHROME_PROFILE / relative
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(item, dst)
                restored.append(str(relative))
            except Exception as e:
                logger.warning(f"Could not restore {relative}: {e}")

    if restored:
        logger.info(f"Restored {len(restored)} files from backup")
        return True

    return False


def reset_profile_with_restore() -> bool:
    """Nuclear option: Reset profile but restore login data from backup."""
    logger.info("Resetting profile with credential restore...")

    backup_path = create_backup()

    if CHROME_PROFILE.exists():
        try:
            shutil.rmtree(CHROME_PROFILE)
        except Exception as e:
            logger.error(f"Could not remove profile: {e}")
            return False

    CHROME_PROFILE.mkdir(parents=True, exist_ok=True)

    if backup_path:
        restore_from_backup(backup_path)

    logger.info("Profile reset complete!")
    return True


# ============================================
# MAIN API FUNCTION
# ============================================

def ensure_healthy_profile(auto_fix: bool = True, create_backup_first: bool = True) -> bool:
    """
    Main function to call before starting Chrome.

    Checks profile health and optionally fixes issues.
    Returns True if profile is healthy (or was fixed).
    """
    logger.info("Checking Chrome profile health...")

    health = check_profile_health()

    if health["exists"]:
        logger.info(f"  Profile size: {health['size_mb']} MB")
        logger.info(f"  Essential files: {len(health['essential_files_present'])} present")

    for warning in health["warnings"]:
        logger.warning(f"  {warning}")

    for issue in health["issues"]:
        logger.error(f"  {issue}")

    if health["healthy"]:
        logger.info("Profile is healthy!")
        return True

    if not auto_fix:
        logger.error("Profile has issues and auto_fix=False")
        return False

    logger.info("Attempting to fix profile issues...")

    if create_backup_first and health["exists"]:
        create_backup()

    if health["lock_status"]["stale_locks"]:
        logger.info("Removing stale lock files...")
        remove_stale_locks()

    if health["size_mb"] > 2000:
        logger.info("Profile is large - clearing caches...")
        clear_cache_dirs()

    health_after = check_profile_health()

    if health_after["healthy"]:
        logger.info("Profile fixed successfully!")
        return True

    logger.warning("Simple fixes didn't work - attempting profile reset with credential restore...")

    if reset_profile_with_restore():
        logger.info("Profile reset successful!")
        return True

    logger.error("Could not fix profile - manual intervention required")
    return False


# ============================================
# CLI INTERFACE
# ============================================

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Chrome Profile Health Manager - Boguszów-Gorce",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --check          Check profile health
  %(prog)s --fix            Check and fix issues
  %(prog)s --backup         Create backup of login credentials
  %(prog)s --restore        Restore from most recent backup
  %(prog)s --reset          Nuclear reset with credential restore
        """
    )

    parser.add_argument("--check", action="store_true",
                       help="Check profile health (read-only)")
    parser.add_argument("--fix", action="store_true",
                       help="Check and fix profile issues")
    parser.add_argument("--backup", action="store_true",
                       help="Create backup of essential files")
    parser.add_argument("--restore", action="store_true",
                       help="Restore from most recent backup")
    parser.add_argument("--reset", action="store_true",
                       help="Reset profile but restore credentials")
    parser.add_argument("--verbose", "-v", action="store_true",
                       help="Verbose output")

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if not any([args.check, args.fix, args.backup, args.restore, args.reset]):
        parser.print_help()
        return

    if args.backup:
        backup_path = create_backup()
        if backup_path:
            print(f"Backup created: {backup_path}")

    if args.restore:
        if restore_from_backup():
            print("Restore successful!")
        else:
            print("Restore failed!")
            sys.exit(1)

    if args.reset:
        if reset_profile_with_restore():
            print("Reset successful!")
        else:
            print("Reset failed!")
            sys.exit(1)

    if args.check:
        health = check_profile_health()
        print(f"\n{'='*50}")
        print("CHROME PROFILE HEALTH CHECK")
        print(f"{'='*50}")
        print(f"Profile: {CHROME_PROFILE}")
        print(f"Exists: {health['exists']}")
        print(f"Healthy: {'YES' if health['healthy'] else 'NO'}")
        print(f"Size: {health['size_mb']} MB")
        print(f"\nLock Status:")
        print(f"  Has locks: {health['lock_status']['has_locks']}")
        print(f"  Stale locks: {health['lock_status']['stale_locks']}")
        print(f"  Active locks: {health['lock_status']['active_locks']}")

        if health['warnings']:
            print(f"\nWarnings:")
            for w in health['warnings']:
                print(f"  - {w}")

        if health['issues']:
            print(f"\nIssues:")
            for i in health['issues']:
                print(f"  - {i}")

        sys.exit(0 if health['healthy'] else 1)

    if args.fix:
        if ensure_healthy_profile(auto_fix=True):
            print("Profile is healthy!")
            sys.exit(0)
        else:
            print("Could not fix profile!")
            sys.exit(1)


if __name__ == "__main__":
    main()
