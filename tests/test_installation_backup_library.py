"""Managed link namespaces preserve library sidecars without following media links."""

import os
from pathlib import Path
import stat
import tempfile
import unittest

from utils.installation_backup_archive import (
    InstallationArchiveError,
    LIBRARY_SIDECAR_SUFFIXES,
    _unsupported_library_file,
    source_rows,
)


class InstallationLibraryCoverageTests(unittest.TestCase):
    def _root(self, path):
        return {"id": "library", "path": str(path), "link_only": True}

    def test_recognized_sidecars_and_large_strm_are_included(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for suffix in LIBRARY_SIDECAR_SUFFIXES:
                (root / ("library" + suffix.upper())).write_bytes(b"sidecar")
            # Sidecar payloads stream like other backed-up files; their size is
            # charged by capacity rather than an unrelated per-file cutoff.
            with (root / "large.strm").open("wb") as handle:
                handle.truncate(2 * 1024 * 1024)
            rows = list(source_rows(self._root(root), content=False))
            names = {Path(row["name"]).name for row, _, _ in rows}
            self.assertIn("library.SRT", names)
            self.assertIn("library.NFO", names)
            self.assertIn("library.JPG", names)
            self.assertIn("large.strm", names)
            self.assertEqual(len(LIBRARY_SIDECAR_SUFFIXES) + 2, len(rows))

    def test_literal_media_symlink_is_recorded_without_following_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            os.symlink("/missing-media/episode.mkv", root / "episode.mkv")
            rows = list(source_rows(self._root(root), content=False))
            linked = next(row for row, _, _ in rows if row.get("link"))
            self.assertTrue(stat.S_ISLNK(linked["mode"]))
            self.assertEqual("/missing-media/episode.mkv", linked["link"])

    def test_regular_media_or_unknown_payload_is_named_and_refused(self):
        for name in ("episode.mkv", "unknown.bin", "extensionless"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / name).write_bytes(b"payload")
                with self.assertRaises(InstallationArchiveError) as caught:
                    list(source_rows(self._root(root), content=False))
                self.assertIn(str(root / name), str(caught.exception))
                self.assertIn("no entries were omitted", str(caught.exception))

    def test_regular_media_file_cannot_be_a_link_namespace_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "episode.mkv"
            path.write_bytes(b"payload")
            with self.assertRaises(InstallationArchiveError):
                list(source_rows(self._root(path), content=False))

    def test_error_path_is_escaped_and_bounded(self):
        message = _unsupported_library_file("library/line\n" + "x" * 5000)
        self.assertNotIn("\n", message)
        self.assertIn("\\n", message)
        self.assertIn("path truncated", message)
        self.assertLess(len(message), 1300)
        self.assertLess(len(_unsupported_library_file("\U0001f600" * 5000)), 1300)


if __name__ == "__main__":
    unittest.main()
