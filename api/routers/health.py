from fastapi import APIRouter, HTTPException
import subprocess
import sys

health_router = APIRouter()
_HEALTHCHECK_TIMEOUT_SECONDS = 8


def _sanitize_health_output(details: str) -> str:
    safe = (details or "").replace("\x00", "")
    safe = " ".join(safe.splitlines())
    return safe[:200]


@health_router.get("")
async def health_check():
    try:
        result = subprocess.run(
            [sys.executable, "/healthcheck.py"],
            capture_output=True,
            text=True,
            timeout=_HEALTHCHECK_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            return {
                "status": "unhealthy",
                "details": _sanitize_health_output(result.stderr),
            }
        return {"status": "healthy"}
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Healthcheck timed out")
    except Exception as e:
        raise HTTPException(
            status_code=500, detail="Failed to run health check. Please try again."
        ) from None
