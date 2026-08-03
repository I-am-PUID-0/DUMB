import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from utils import rclone_optimizer


class _ConfigManager:
    def __init__(self, cache_dir):
        self.instance = {
            "enabled": True,
            "process_name": "rclone w/ NzbDAV",
            "key_type": "nzbdav",
            "mount_dir": "/mnt/debrid",
            "mount_name": "nzbdav",
            "cache_dir": str(cache_dir),
            "command": [
                "rclone",
                "mount",
                "nzbdav:",
                "/mnt/debrid/nzbdav",
                "--config=/config/rclone.config",
                "--vfs-cache-mode=full",
                "--buffer-size=1024M",
                "--links",
            ],
        }

    def get(self, key, default=None):
        if key == "rclone":
            return {"instances": {"NzbDAV": self.instance}}
        if key in {"puid", "pgid"}:
            return 1000
        return default

    def save_config(self, _process_name=None):
        return None


class RcloneOptimizerTests(unittest.TestCase):
    def test_managed_flag_merge_preserves_user_flags_and_paths(self):
        command = [
            "rclone",
            "mount",
            "nzbdav:",
            "/mnt/debrid/nzbdav",
            "--config=/config/rclone.config",
            "--buffer-size=1024M",
            "--links",
        ]

        merged = rclone_optimizer.merge_managed_flags(
            command,
            {"--buffer-size": "64M", "--vfs-read-ahead": "128M"},
        )

        self.assertEqual(command[:4], merged[:4])
        self.assertIn("--config=/config/rclone.config", merged)
        self.assertIn("--links", merged)
        self.assertIn("--buffer-size=64M", merged)
        self.assertIn("--vfs-read-ahead=128M", merged)
        self.assertNotIn("--buffer-size=1024M", merged)

    def test_candidate_matrix_is_bounded(self):
        limits = {"max_vfs_cache_gib": 7}

        quick = rclone_optimizer._candidate_profiles("quick", limits)
        standard = rclone_optimizer._candidate_profiles("standard", limits)
        thorough = rclone_optimizer._candidate_profiles("thorough", limits)

        self.assertEqual(2, len(quick))
        self.assertEqual(4, len(standard))
        self.assertEqual(6, len(thorough))
        self.assertTrue(
            all(
                candidate["settings"].get("--vfs-cache-max-size") == "7G"
                for candidate in thorough
            )
        )
        self.assertEqual("Current tuning (bounded cache)", quick[0]["label"])

    def test_shadow_command_is_read_only_isolated_and_loopback_only(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _ConfigManager(root / "cache")
            with patch.object(rclone_optimizer, "CONFIG_MANAGER", config):
                manager = rclone_optimizer.RcloneOptimizerManager(
                    process_handler=object(),
                    logger=unittest.mock.Mock(),
                    base_dir=str(root / "optimizer"),
                )
                command = manager._shadow_command(
                    config.instance,
                    {
                        "settings": {
                            "--buffer-size": "32M",
                            "--vfs-cache-max-size": "5G",
                        }
                    },
                    root / "mount",
                    root / "shadow-cache",
                    45678,
                    80,
                )

        self.assertEqual(str(root / "mount"), command[3])
        self.assertIn(f"--cache-dir={root / 'shadow-cache'}", command)
        self.assertIn("--rc-addr=127.0.0.1:45678", command)
        self.assertIn("--rc-no-auth", command)
        self.assertIn("--read-only", command)
        self.assertIn("--bwlimit=10M", command)
        self.assertNotIn("--buffer-size=1024M", command)

    def test_public_job_never_exposes_commands(self):
        public = rclone_optimizer.RcloneOptimizerManager._public(
            {
                "job_id": "a" * 32,
                "previous_command": ["--rc-pass=secret"],
                "recommended_command": ["--rc-pass=secret"],
                "status": "completed",
            }
        )

        self.assertNotIn("previous_command", public)
        self.assertNotIn("recommended_command", public)
        self.assertNotIn("secret", json.dumps(public))

    def test_active_persisted_job_is_marked_interrupted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            jobs = root / "optimizer" / "jobs"
            jobs.mkdir(parents=True)
            job_id = "b" * 32
            (jobs / f"{job_id}.json").write_text(
                json.dumps(
                    {
                        "job_id": job_id,
                        "process_name": "rclone w/ NzbDAV",
                        "status": "benchmarking",
                        "created_at": "2026-01-01T00:00:00+00:00",
                    }
                ),
                encoding="utf-8",
            )
            config = _ConfigManager(root / "cache")
            with patch.object(rclone_optimizer, "CONFIG_MANAGER", config):
                manager = rclone_optimizer.RcloneOptimizerManager(
                    process_handler=object(),
                    logger=unittest.mock.Mock(),
                    base_dir=str(root / "optimizer"),
                )

            loaded = manager.get_job(job_id)

        self.assertEqual("interrupted", loaded["status"])
        self.assertIn("not resumed", loaded["error"])

    def test_nzbdav_summary_includes_provider_health(self):
        summary = rclone_optimizer.RcloneOptimizerManager._summarize_nzbdav(
            {
                "window": "1h",
                "tiles": {
                    "activeReads": 2,
                    "errorsPerMinute": 1,
                    "bytesServedPerMinute": 100,
                    "inFlightArticleThrottleEvents": 3,
                },
                "latency": {"p50Ms": 10, "p95Ms": 30, "p99Ms": 60},
                "providers": [
                    {
                        "nickname": "primary",
                        "bytesFetched": 200,
                        "errors": 1,
                        "retries": 2,
                        "avgDurationMs": 15,
                        "circuitState": "closed",
                    }
                ],
            }
        )

        self.assertEqual(2, summary["active_reads"])
        self.assertEqual(30, summary["provider_latency_p95_ms"])
        self.assertEqual("closed", summary["providers"][0]["circuit_state"])

    def test_read_sample_obeys_shared_download_budget(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "sample.mkv"
            path.write_bytes(b"x" * (4 * 1024 * 1024))
            budget = {"remaining": 2 * 1024 * 1024}

            sample = rclone_optimizer.RcloneOptimizerManager._read_sample(
                path,
                {
                    "path": "sample.mkv",
                    "size_bytes": path.stat().st_size,
                    "age_bucket": "older",
                },
                1024 * 1024,
                budget,
                threading.Lock(),
                time.monotonic() + 10,
                threading.Event(),
            )

        self.assertTrue(sample["scored"])
        self.assertLessEqual(sample["bytes_read"], 2 * 1024 * 1024)
        self.assertEqual(0, budget["remaining"])
        self.assertGreaterEqual(sample["startup_ms"], sample["ttfb_ms"])

    def test_provider_guard_uses_test_window_deltas(self):
        before = {
            "totalErrors": 20,
            "tiles": {"inFlightArticleThrottleEvents": 1},
            "providers": [{"errors": 20, "retries": 30, "circuitState": "closed"}],
            "failover": {"readsSaved": 10},
        }
        normal_after = {
            "totalErrors": 20,
            "tiles": {"inFlightArticleThrottleEvents": 1},
            "providers": [{"errors": 20, "retries": 31, "circuitState": "closed"}],
            "failover": {"readsSaved": 10},
        }
        throttled_after = {
            **normal_after,
            "tiles": {"inFlightArticleThrottleEvents": 2},
        }

        self.assertFalse(
            rclone_optimizer.RcloneOptimizerManager._provider_guard(
                before, normal_after, {"sessions": []}
            )
        )
        self.assertTrue(
            rclone_optimizer.RcloneOptimizerManager._provider_guard(
                before, throttled_after, {"sessions": []}
            )
        )
        self.assertFalse(
            rclone_optimizer.RcloneOptimizerManager._provider_guard(
                {"totalErrors": 0, "providers": [{"errors": 0}]},
                {"totalErrors": 3, "providers": [{"errors": 3}]},
                [],
            )
        )

    def test_trace_summary_matches_test_content_and_omits_client_metadata(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _ConfigManager(root / "cache")
            with patch.object(rclone_optimizer, "CONFIG_MANAGER", config):
                manager = rclone_optimizer.RcloneOptimizerManager(
                    process_handler=object(),
                    logger=unittest.mock.Mock(),
                    base_dir=str(root / "optimizer"),
                )
            manager._nzbdav_json = unittest.mock.Mock(
                return_value={
                    "events": [
                        {
                            "kind": "ProviderFetch",
                            "provider": "primary",
                            "status": "Ok",
                            "retries": 1,
                            "bytesServed": 1024,
                            "providerWaitMs": 20,
                            "clientIp": "192.0.2.1",
                            "userAgent": "private-client",
                        }
                    ]
                }
            )
            session_id = "12345678-1234-4234-9234-123456789abc"

            summary = manager._collect_trace_summaries(
                {
                    "sessions": [
                        {
                            "sessionId": session_id,
                            "path": "/movies/Example%20Movie.mkv",
                            "firstAt": 10_001,
                            "lastAt": 10_100,
                            "eventCount": 1,
                        },
                        {
                            "sessionId": "aaaaaaaa-1234-4234-9234-123456789abc",
                            "path": "/shows/Example%20Movie.mkv",
                            "firstAt": 10_002,
                            "lastAt": 10_101,
                            "eventCount": 1,
                        },
                    ]
                },
                ["movies/Example Movie.mkv"],
                10_000,
            )

        self.assertEqual(1, len(summary))
        self.assertEqual(["primary"], summary[0]["providers"])
        self.assertEqual({"Ok": 1}, summary[0]["statuses"])
        self.assertNotIn("clientIp", json.dumps(summary))
        self.assertNotIn("private-client", json.dumps(summary))

    def test_empty_previous_command_is_still_rollback_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _ConfigManager(root / "cache")
            with patch.object(rclone_optimizer, "CONFIG_MANAGER", config):
                manager = rclone_optimizer.RcloneOptimizerManager(
                    process_handler=object(),
                    logger=unittest.mock.Mock(),
                    base_dir=str(root / "optimizer"),
                )
                job_id = "c" * 32
                manager._jobs[job_id] = {
                    "job_id": job_id,
                    "process_name": "rclone w/ NzbDAV",
                    "status": "applied",
                    "previous_command": [],
                    "recommendation": {"applied": True},
                }
                with patch.object(manager, "_restore_previous") as restore:
                    result = manager.rollback(job_id)

        restore.assert_called_once()
        self.assertEqual("rolled_back", result["status"])


if __name__ == "__main__":
    unittest.main()
