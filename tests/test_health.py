import asyncio
import types
import unittest
from unittest.mock import patch

from api.routers import health as health_router


from fastapi import HTTPException


class HealthRouterTests(unittest.TestCase):
    def test_health_returns_healthy_when_healthcheck_succeeds(self):
        result = types.SimpleNamespace(returncode=0, stderr="")

        with patch.object(health_router.subprocess, "run", return_value=result) as run:
            response = asyncio.run(health_router.health_check())

        run.assert_called_once()
        called_args, called_kwargs = run.call_args
        self.assertEqual(called_args[0][0], health_router.sys.executable)
        self.assertEqual(called_args[0][1], "/healthcheck.py")
        self.assertTrue(called_kwargs["capture_output"])
        self.assertTrue(called_kwargs["text"])
        self.assertEqual(called_kwargs["timeout"], 8)
        self.assertEqual(response, {"status": "healthy"})

    def test_health_reports_unhealthy_on_non_zero_exit_code(self):
        result = types.SimpleNamespace(returncode=1, stderr="boom")

        with patch.object(health_router.subprocess, "run", return_value=result) as run:
            response = asyncio.run(health_router.health_check())

        run.assert_called_once()
        self.assertEqual(response, {"status": "unhealthy", "details": "boom"})

    def test_health_enforces_timeout(self):
        result = types.SimpleNamespace(returncode=0, stderr="")

        with patch.object(health_router.subprocess, "run", return_value=result) as run:
            asyncio.run(health_router.health_check())

        called_kwargs = run.call_args.kwargs
        self.assertEqual(called_kwargs["timeout"], 8)

    def test_health_sanitizes_newlines_in_stderr(self):
        result = types.SimpleNamespace(returncode=1, stderr="boom\nline2\nline3")

        with patch.object(health_router.subprocess, "run", return_value=result) as run:
            response = asyncio.run(health_router.health_check())

        run.assert_called_once()
        self.assertNotIn("\n", response["details"])

    def test_health_maps_timeout_to_504(self):
        with patch.object(
            health_router.subprocess,
            "run",
            side_effect=health_router.subprocess.TimeoutExpired(
                cmd=[health_router.sys.executable, "/healthcheck.py"], timeout=8
            ),
        ):
            with self.assertRaises(HTTPException) as exc:
                asyncio.run(health_router.health_check())

        self.assertEqual(exc.exception.status_code, 504)


if __name__ == "__main__":
    unittest.main()
