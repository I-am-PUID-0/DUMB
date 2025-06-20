from fastapi import APIRouter, HTTPException
import subprocess

health_router = APIRouter()


@health_router.get("")
async def health_check():
    try:
        result = subprocess.run(
            ["bash", "-c", "source /venv/bin/activate && python /healthcheck.py"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return {"status": "unhealthy", "details": result.stderr.strip()}
        return {"status": "healthy"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to run health check: {e}")
