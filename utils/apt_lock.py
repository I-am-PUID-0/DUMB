from contextlib import contextmanager
import threading
import time
import subprocess

from utils.global_logger import logger


_APT_LOCK = threading.Lock()


def _looks_like_lock_error(stderr: str) -> bool:
    if not stderr:
        return False
    needle = stderr.lower()
    return (
        "dpkg frontend lock" in needle
        or "could not get lock" in needle
        or "unable to acquire the dpkg frontend lock" in needle
        or "could not acquire dpkg frontend lock" in needle
    )


@contextmanager
def apt_lock():
    _APT_LOCK.acquire()
    try:
        yield
    finally:
        _APT_LOCK.release()


def run_locked(cmd, *, retries: int = 6, delay_s: float = 5.0, **kwargs):
    with apt_lock():
        last_err = None
        for attempt in range(max(1, retries)):
            try:
                return subprocess.run(cmd, **kwargs)
            except subprocess.CalledProcessError as e:
                stderr = getattr(e, "stderr", "") or ""
                if not _looks_like_lock_error(stderr) or attempt == retries - 1:
                    raise
                wait_s = delay_s * (attempt + 1)
                logger.warning(
                    "dpkg/apt lock busy; retrying in %.1fs (%s)",
                    wait_s,
                    cmd,
                )
                time.sleep(wait_s)
                last_err = e
        if last_err:
            raise last_err
