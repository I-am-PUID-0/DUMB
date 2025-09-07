import os
import stat
from pathlib import Path
from utils.global_logger import logger


def check_and_fix_permissions():
    """Check if files exist and fix permissions. Returns True if successful."""
    files = ["/cli_debrid/utilities/bulk_subs.sh", "/cli_debrid/utilities/downsub.sh"]

    if not all(Path(f).exists() for f in files):
        return False

    success = True
    for file_path in files:
        try:
            os.chown(file_path, 1000, 1000)
            current_mode = Path(file_path).stat().st_mode
            Path(file_path).chmod(
                current_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
            )
            logger.debug(f"Fixed permissions for {file_path}")
        except Exception as e:
            logger.error(f"Error setting permissions for {file_path}: {e}")
            success = False

    if success:
        logger.info("CLI Debrid subscription scripts permissions fixed successfully")

    return success


def start_permission_monitor(max_attempts=60, delay=5):
    """Start monitoring for CLI Debrid files and fix permissions when they appear."""
    import time

    for attempt in range(max_attempts):
        time.sleep(delay)
        if check_and_fix_permissions():
            break
    else:
        logger.warning("Failed to fix CLI Debrid permissions after timeout")
