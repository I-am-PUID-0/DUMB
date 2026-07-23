import tempfile
import unittest
from pathlib import Path

from api.routers.logs import _read_complete_chunk
from utils.logger import redact_sensitive_log_data


class LogChunkRedactionTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
