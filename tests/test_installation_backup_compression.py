"""Compressed and legacy owned archives retain full verification and recovery."""

import hashlib
import io
import os
import sqlite3
import tarfile
import unittest
from unittest.mock import patch

from tests import test_installation_backup as fixtures
from utils import installation_backup as manager_module
from utils import installation_backup_codec as codec
from utils.backup_storage import BackupStorageError
from utils.installation_backup_archive import (
    InstallationArchiveError,
    inspect_archive,
    write_archive,
)


class CompressionArchiveTests(unittest.TestCase):
    setUp = fixtures.ArchiveTests.setUp

    def output(self, compression="zstd", progress=None):
        output = io.BytesIO()
        write_archive(
            output,
            self.metadata,
            self.roots,
            self.scratch,
            compression=compression,
            progress=progress,
        )
        self.assertFalse(output.closed)
        return output.getvalue()

    def test_direct_stream_roundtrip_matches_legacy_tar_and_raw_byte_progress(self):
        payload = b"reserved-compressed-library-state:" * 100000
        self.source.joinpath("payload").write_bytes(payload)
        os.symlink("/reserved/library/item.mkv", self.source / "item.mkv")
        plain = self.output("none")
        events = []
        compressed = self.output(progress=lambda **row: events.append(row))
        self.assertEqual(codec.MAGIC, bytes.fromhex("28b52ffd"))
        self.assertEqual(len(codec.MAGIC), 4)
        self.assertTrue(compressed.startswith(codec.MAGIC))
        self.assertLess(len(compressed), len(plain))
        self.assertEqual(codec.zstd.decompress(compressed), plain)
        target = self.root / "restored"
        target.mkdir()
        reads = []
        proof = inspect_archive(
            io.BytesIO(compressed),
            self.scratch,
            extract_root=target,
            progress=lambda **row: reads.append(row),
        )
        self.assertTrue(proof["verified"])
        self.assertEqual(proof["compression"], "zstd")
        self.assertEqual((target / "root_000/payload").read_bytes(), payload)
        self.assertEqual(
            os.readlink(target / "root_000/item.mkv"), "/reserved/library/item.mkv"
        )
        streamed = [row for row in events if row["stage"] == "archiving"]
        self.assertEqual(streamed[-1]["bytes_processed"], len(compressed))
        self.assertEqual(reads[-1]["bytes_processed"], len(compressed))
        self.assertEqual(reads[-1]["logical_bytes"], len(payload))
        self.assertEqual(
            inspect_archive(io.BytesIO(plain), self.scratch)["compression"], "none"
        )
        self.assertEqual(list(self.scratch.glob(".installation-manifest-*")), [])

    def test_truncation_checksum_trailing_and_second_frame_are_rejected(self):
        self.source.joinpath("payload").write_bytes(b"reserved" * 1000)
        raw = self.output()
        damaged = bytearray(raw)
        damaged[-1] ^= 1
        for label, value in [
            ("truncated checksum", raw[:-1]),
            ("truncated frame", raw[:-5]),
            ("checksum mismatch", bytes(damaged)),
            ("trailing zeros", raw + bytes(32)),
            ("second frame", raw + codec.zstd.compress(b"")),
        ]:
            with self.subTest(case=label), self.assertRaises(InstallationArchiveError):
                inspect_archive(io.BytesIO(value), self.scratch)
            self.assertEqual(list(self.scratch.glob(".installation-manifest-*")), [])

    def test_compressed_zero_tail_stops_at_the_tar_record_envelope(self):
        self.source.joinpath("payload").write_bytes(b"reserved")
        plain = self.output("none")
        compressed = codec.zstd.compress(plain + bytes(12 * codec.CHUNK))
        original = codec._ZstdReader.read
        returned = []

        def read(stream, amount):
            data = original(stream, amount)
            returned.append(len(data))
            return data

        with patch.object(codec._ZstdReader, "read", read):
            with self.assertRaisesRegex(InstallationArchiveError, "record envelope"):
                inspect_archive(io.BytesIO(compressed), self.scratch)
        self.assertLessEqual(sum(returned), len(plain) + tarfile.RECORDSIZE + 1)
        self.assertLess(sum(returned), 12 * codec.CHUNK)
        self.assertEqual(list(self.scratch.glob(".installation-manifest-*")), [])

    def test_valid_tar_record_boundaries_remain_accepted_for_both_formats(self):
        padding = set()
        path = self.source / "payload"
        for size in range(0, 2 * tarfile.RECORDSIZE + 1, tarfile.BLOCKSIZE):
            path.write_bytes(b"x" * size)
            plain = self.output("none")
            self.assertEqual(len(plain) % tarfile.RECORDSIZE, 0)
            with tarfile.open(fileobj=io.BytesIO(plain), mode="r:") as archive:
                final = archive.getmembers()[-1]
                end = (
                    final.offset_data
                    + ((final.size + tarfile.BLOCKSIZE - 1) // tarfile.BLOCKSIZE)
                    * tarfile.BLOCKSIZE
                )
                padding.add(len(plain) - end)
            for raw in (plain, codec.zstd.compress(plain)):
                proof = inspect_archive(io.BytesIO(raw), self.scratch)
                self.assertTrue(proof["verified"])
        self.assertLessEqual(min(padding), 2 * tarfile.BLOCKSIZE)
        self.assertGreaterEqual(max(padding), tarfile.RECORDSIZE)
        self.assertEqual(list(self.scratch.glob(".installation-manifest-*")), [])

    def test_decoder_is_bounded_even_for_high_expansion_and_short_raw_reads(self):
        payload = b"x" * (3 * codec.CHUNK + 29)
        source = io.BytesIO(codec.zstd.compress(payload))
        observed = []

        class ShortReads:
            def read(self, amount):
                observed.append(amount)
                return source.read(min(amount, 43))

        decoded, kind = codec.decoded_input(ShortReads())
        result = []
        while chunk := decoded.read(3 * codec.CHUNK):
            self.assertLessEqual(len(chunk), codec.CHUNK)
            result.append(chunk)
        self.assertEqual(kind, "zstd")
        self.assertEqual(b"".join(result), payload)
        self.assertTrue(decoded.complete)
        self.assertTrue(all(0 < amount <= codec.CHUNK for amount in observed))
        with self.assertRaisesRegex(codec.InstallationCodecError, "bounded"):
            decoded.read(-1)

    def test_decoder_refuses_a_frame_above_its_window_budget(self):
        compressor = codec.zstd.ZstdCompressor(
            options={
                codec.zstd.CompressionParameter.compression_level: 1,
                codec.zstd.CompressionParameter.window_log: 27,
            }
        )
        compressor.set_pledged_input_size(1 << 30)
        header = compressor.compress(b"reserved" * 20000)
        self.assertTrue(header.startswith(codec.MAGIC))
        with self.assertRaisesRegex(InstallationArchiveError, "window check"):
            inspect_archive(io.BytesIO(header), self.scratch)

    def test_unavailable_codec_never_silently_writes_another_format(self):
        with patch.object(codec, "zstd", None):
            self.assertFalse(codec.zstd_available())
            with self.assertRaisesRegex(InstallationArchiveError, "unavailable"):
                self.output()
            with self.assertRaisesRegex(InstallationArchiveError, "unavailable"):
                inspect_archive(io.BytesIO(codec.MAGIC + b"reserved"), self.scratch)
            self.assertEqual(
                inspect_archive(io.BytesIO(self.output("none")), self.scratch)[
                    "compression"
                ],
                "none",
            )


class CompressionManagerTests(unittest.TestCase):
    setUp = fixtures.ManagerTests.setUp
    join_worker = fixtures.ManagerTests.join_worker
    start_backup = fixtures.ManagerTests.start_backup
    restore_request = fixtures.ManagerTests.restore_request

    def request(self, compression):
        preview = self.manager.preview(
            {"destination_id": "fixture", "compression": compression}
        )
        self.assertTrue(preview["ready"], preview["blockers"])
        self.assertEqual(preview["compression"], compression)
        self.assertEqual(
            self.manager.preview_status()["preview"]["compression"], compression
        )
        return {
            "preview_token": preview["preview_token"],
            "confirmation": preview["confirmation_text"],
            "acknowledgements": {
                "downtime": True,
                "secrets": True,
                "external_state": True,
            },
        }

    def test_owned_compressed_receipt_hash_and_real_restore_rollback(self):
        request = self.request("zstd")
        result = self.manager.start(request)
        self.join_worker()
        backup = self.manager.get_job(result["job"]["job_id"])["job"]
        self.assertEqual(backup["status"], "completed", backup)
        receipt = backup["result"]["archive"]
        self.assertEqual(receipt["extension"], "tar.zst")
        self.assertEqual(receipt["compression"], "zstd")
        with self.storage.open_artifact(receipt["reference"]) as handle:
            raw = handle.read()
        self.assertEqual(receipt["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(receipt["size_bytes"], len(raw))
        self.assertEqual(backup["progress"]["bytes_processed"], len(raw))
        self.assertEqual(
            self.manager.catalog("fixture")["artifacts"][0]["compression"], "zstd"
        )
        with sqlite3.connect(self.payload) as database:
            database.execute("UPDATE records SET value='after'")
        preview = self.manager.restore_preview({"reference": receipt["reference"]})
        self.assertTrue(preview["ready"], preview["blockers"])
        self.assertEqual(preview["backup"]["compression"], "zstd")
        restored = self.manager.restore_start(
            {
                "preview_token": preview["preview_token"],
                "confirmation": preview["confirmation_text"],
                "acknowledgements": {
                    "downtime": True,
                    "replace_state": True,
                    "external_state": True,
                },
            }
        )
        self.join_worker()
        identifier = restored["job"]["job_id"]
        self.assertEqual(self.manager.get_job(identifier)["job"]["status"], "completed")
        with sqlite3.connect(self.payload) as database:
            self.assertEqual(
                database.execute("SELECT value FROM records").fetchone(), ("before",)
            )
        self.manager.rollback(identifier, {"confirmation": "ROLL BACK " + identifier})
        with sqlite3.connect(self.payload) as database:
            self.assertEqual(
                database.execute("SELECT value FROM records").fetchone(), ("after",)
            )

    def test_import_sniffs_compression_without_filename_hint(self):
        for compression, filename in (
            ("zstd", "reserved-import.tar"),
            ("none", "reserved-import.tar.zst"),
        ):
            with self.subTest(compression=compression):
                result = self.manager.start(self.request(compression))
                self.join_worker()
                job = self.manager.get_job(result["job"]["job_id"])["job"]
                path = self.root / filename
                with self.storage.open_artifact(
                    job["result"]["archive"]["reference"]
                ) as stream:
                    path.write_bytes(stream.read())
                path.chmod(0o600)
                preview = self.manager.restore_preview({"archive_path": str(path)})
                self.assertTrue(preview["ready"], preview["blockers"])
                self.assertEqual(preview["backup"]["compression"], compression)

    def test_truncated_compressed_producer_never_publishes_a_receipt_or_partial(self):
        # Use a real valid installation tar from the current fixture's ordinary
        # backup, then truncate only its compressed frame checksum.
        backup = self.start_backup()
        reference = backup["result"]["archive"]["reference"]
        with self.storage.open_artifact(reference) as handle:
            truncated = codec.zstd.compress(handle.read())[:-1]
        self.storage.delete_artifact(reference)
        scratch = self.manager._workspace("preview")
        with self.assertRaisesRegex(InstallationArchiveError, "truncated"):
            self.storage.write_file(
                "fixture",
                "installation",
                lambda handle: handle.write(truncated),
                extension="tar.zst",
                verifier=lambda handle: inspect_archive(handle, scratch),
                metadata={"kind": "installation_backup", "compression": "zstd"},
            )
        self.assertEqual(self.manager.catalog("fixture")["artifacts"], [])
        self.assertEqual(list(self.root.joinpath("archives").rglob("*.partial")), [])

    def test_capacity_never_assumes_compression_savings_and_rechecks_bound_codec(self):
        options = {"destination_id": "fixture"}
        plain = self.manager.preview(options)
        compressed = self.manager.preview(options | {"compression": "zstd"})
        first = plain["capacity"]["source"]["estimated_archive_bytes"]
        second = compressed["capacity"]["source"]["estimated_archive_bytes"]
        self.assertGreater(second, first)
        self.assertEqual(
            compressed["capacity"]["source"]["estimated_uncompressed_archive_bytes"],
            first,
        )
        self.assertEqual(compressed["capacity"]["source"]["compression"], "zstd")
        original = manager_module.capacity
        seen = []

        def capture(*args, **kwargs):
            seen.append(kwargs.get("compression"))
            return original(*args, **kwargs)

        request = {
            "preview_token": compressed["preview_token"],
            "confirmation": compressed["confirmation_text"],
            "acknowledgements": {
                "downtime": True,
                "secrets": True,
                "external_state": True,
            },
        }
        with patch.object(manager_module, "capacity", capture):
            result = self.manager.start(request)
            self.join_worker()
        self.assertEqual(seen, ["zstd"])
        self.assertEqual(
            self.manager._job(result["job"]["job_id"])["plan"]["compression"], "zstd"
        )

    def test_legacy_schedule_save_preserves_compression_and_tick_uses_it(self):
        self.manager.update_schedule(
            {
                "enabled": True,
                "destination_id": "fixture",
                "compression": "zstd",
                "interval_hours": 1,
                "keep_last": 2,
            }
        )
        result = self.manager.update_schedule({"enabled": True, "interval_hours": 2})
        self.assertEqual(result["schedule"]["compression"], "zstd")
        self.manager._write(self.manager._state("schedule.json"), {"next_due_at": 0})
        self.manager.tick()
        self.join_worker()
        job = self.manager.status()["job"]
        self.assertTrue(self.manager._job(job["job_id"])["scheduled"])
        self.assertEqual(job["status"], "completed", job)
        self.assertEqual(job["result"]["archive"]["compression"], "zstd")

    def test_tar_zstd_extension_is_closed_to_installation_namespace(self):
        with self.assertRaisesRegex(BackupStorageError, "unsupported"):
            self.storage.write_file(
                "fixture", "symlinks", lambda _: None, extension="tar.zst"
            )

    def test_invalid_compression_is_not_an_actionable_review(self):
        preview = self.manager.preview(
            {"destination_id": "fixture", "compression": "gzip"}
        )
        self.assertFalse(preview["ready"])
        self.assertIn("Choose none or zstd", " ".join(preview["blockers"]))
        self.assertEqual(self.handler.events, [])


if __name__ == "__main__":
    unittest.main()
