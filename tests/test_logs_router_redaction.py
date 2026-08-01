import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from api.routers.logs import (
    _bounded_log_start,
    _is_dumb_api_process,
    _read_complete_chunk,
    _read_log_snapshot,
    find_log_file,
    get_log_file,
)
from utils.logger import CustomRotatingFileHandler, redact_sensitive_log_data


class LogChunkRedactionTests(unittest.TestCase):
    def test_dumb_frontend_is_not_treated_as_the_dumb_api_log(self):
        self.assertTrue(_is_dumb_api_process("DUMB API"))
        self.assertTrue(_is_dumb_api_process("dumb_api_service"))
        self.assertFalse(_is_dumb_api_process("DUMB Frontend"))

    def test_dumb_frontend_uses_its_configured_service_log(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            frontend_log = Path(temp_dir) / "dumb_frontend.log"
            frontend_log.write_text("frontend output\n")

            with (
                patch(
                    "api.routers.logs.CONFIG_MANAGER.find_key_for_process",
                    return_value=("dumb_frontend", None),
                ),
                patch(
                    "api.routers.logs.CONFIG_MANAGER.get_instance",
                    return_value={"log_file": str(frontend_log)},
                ),
            ):
                resolved = find_log_file("DUMB Frontend", Mock())

            self.assertEqual(resolved, frontend_log)

    def test_bounds_a_large_incremental_backlog(self):
        self.assertEqual(_bounded_log_start(10_000, 1_000, 2_000), (8_000, True))

    def test_keeps_a_small_incremental_backlog(self):
        self.assertEqual(_bounded_log_start(10_000, 9_000, 2_000), (9_000, False))

    def test_resets_after_rotation(self):
        self.assertEqual(_bounded_log_start(1_000, 10_000, 2_000), (0, True))

    def test_holds_partial_sensitive_line_until_it_is_complete(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "service.log"
            path.write_bytes(b"safe line\nCookie: session=partial")

            data, cursor = _read_complete_chunk(path, 0)

            self.assertEqual(data, b"safe line\n")
            self.assertEqual(cursor, len(b"safe line\n"))

            with path.open("ab") as handle:
                handle.write(b"-value\n")

            data, cursor = _read_complete_chunk(path, cursor)

            self.assertEqual(
                redact_sensitive_log_data(data.decode()),
                "Cookie: [REDACTED]\n",
            )
            self.assertEqual(cursor, path.stat().st_size)

    def test_skips_a_tail_cursor_that_starts_inside_a_log_line(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "service.log"
            path.write_bytes(b"first\nCookie: session=example\nlast\n")

            data, cursor = _read_complete_chunk(path, len(b"first\nCook"))

            self.assertEqual(data, b"last\n")
            self.assertEqual(cursor, path.stat().st_size)

    def test_snapshot_size_bounds_a_read_while_file_grows(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "service.log"
            path.write_bytes(b"first\nsecond\n")
            snapshot_size = path.stat().st_size

            with path.open("ab") as handle:
                handle.write(b"third\n")

            data, cursor = _read_complete_chunk(path, 0, snapshot_size)

            self.assertEqual(data, b"first\nsecond\n")
            self.assertEqual(cursor, snapshot_size)

    def test_snapshot_size_bounds_partial_line_skipping(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "service.log"
            path.write_bytes(b"first\npartial")
            snapshot_size = path.stat().st_size

            with path.open("ab") as handle:
                handle.write(b"-later\ncomplete\n")

            data, cursor = _read_complete_chunk(
                path,
                len(b"first\npar"),
                snapshot_size,
            )

            self.assertEqual(data, b"")
            self.assertEqual(cursor, snapshot_size)

    def test_file_identity_detects_rotation_even_when_new_file_is_larger(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "service.log"
            path.write_bytes(b"old one\nold two\n")
            _, old_cursor, _, old_file_id, _ = _read_log_snapshot(path, None, 1024)

            rotated = path.with_suffix(".log.1")
            path.replace(rotated)
            path.write_bytes(b"new one\nnew two\nnew three\n")

            data, cursor, size, new_file_id, reset = _read_log_snapshot(
                path,
                old_cursor,
                1024,
                expected_file_id=old_file_id,
            )

            self.assertTrue(reset)
            self.assertNotEqual(new_file_id, old_file_id)
            self.assertEqual(data, path.read_bytes())
            self.assertEqual(cursor, size)

    def test_incremental_rotation_reset_does_not_duplicate_compat_log(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "service.log"
            path.write_bytes(b"old one\nold two\n")
            _, old_cursor, _, old_file_id, _ = _read_log_snapshot(path, None, 1024)
            path.replace(path.with_suffix(".log.1"))
            path.write_bytes(b"new one\nnew two\n")

            with patch("api.routers.logs.find_log_file", return_value=path):
                result = asyncio.run(
                    get_log_file(
                        process_name="Example Service",
                        cursor=old_cursor,
                        tail_bytes=1024,
                        file_id=old_file_id,
                        logger=Mock(),
                        current_user=None,
                    )
                )

            self.assertTrue(result["reset"])
            self.assertEqual(result["chunk"], "new one\nnew two\n")
            self.assertNotIn("log", result)

    def test_real_handler_rollover_returns_only_bounded_replacement_tail(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "service.log"
            handler = CustomRotatingFileHandler(
                path,
                maxBytes=1024,
                backupCount=2,
            )
            handler.logger.disabled = True
            try:
                handler.stream.write("old\n" * 2048)
                handler.stream.flush()
                _, old_cursor, _, old_file_id, _ = _read_log_snapshot(path, None, 1024)

                handler.doRollover()
                with path.open("a") as replacement:
                    replacement.write("new\n" * 4096)

                data, cursor, size, new_file_id, reset = _read_log_snapshot(
                    path,
                    old_cursor,
                    1024,
                    expected_file_id=old_file_id,
                )
            finally:
                handler.close()
                handler.logger.disabled = False

            self.assertTrue(reset)
            self.assertNotEqual(new_file_id, old_file_id)
            self.assertLessEqual(len(data), 1024)
            self.assertEqual(cursor, size)


if __name__ == "__main__":
    unittest.main()
