"""A paused migration retries readback without recreating a completed archive."""

import errno
import hashlib
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from utils import backup_storage as storage_module
from utils import infinidysk_v2_archive as archive_module
from utils.backup_storage import BackupStorage, BackupStorageError
from utils.infinidysk_v2_inventory import tree_fingerprint


class BackupStorageStageRetryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="dumb-readback-retry-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.destination = self.root / "archive"
        self.destination.mkdir(mode=0o700)
        config = SimpleNamespace(
            config={"backup_storage": {"default_destination_id": "", "profiles": {}}},
            file_path=str(self.root / "dumb.json"),
            save_config=Mock(),
        )
        self.storage = BackupStorage(config, state_root=self.root / "control")
        self.storage.save_profile(
            {
                "id": "test",
                "name": "Migration recovery",
                "path": str(self.destination),
                "enabled": True,
            }
        )
        self.payload = b"retained original recovery data" * 100
        self.producer = Mock(side_effect=lambda handle: handle.write(self.payload))

    def test_retry_reopens_the_same_closed_artifact_and_resets_digest(self):
        attempts = []

        def verify(reader):
            attempts.append(reader)
            if len(attempts) == 1:
                reader.read(19)
                raise OSError(errno.ETIMEDOUT, "temporary storage read failure")
            self.assertEqual(reader.read(23), self.payload[:23])
            return {"verified_fixture": True}

        def retry(callback):
            with self.assertRaises(OSError):
                callback()
            partial = list(self.destination.rglob("*.partial"))
            self.assertEqual(len(partial), 1)
            before = partial[0].stat()
            result = callback()
            after = partial[0].stat()
            self.assertEqual(
                (before.st_dev, before.st_ino), (after.st_dev, after.st_ino)
            )
            return result

        receipt = self.storage.write_file(
            "test", "major-test", self.producer, verifier=verify, readback_retry=retry
        )
        self.assertEqual(self.producer.call_count, 1)
        self.assertEqual(len(attempts), 2)
        self.assertIsNot(attempts[0], attempts[1])
        self.assertEqual(receipt["size_bytes"], len(self.payload))
        self.assertEqual(receipt["sha256"], hashlib.sha256(self.payload).hexdigest())
        self.assertTrue(receipt["verified"])
        self.assertFalse(list(self.destination.rglob("*.partial")))
        self.assertEqual(len(list(self.destination.rglob("*.tar"))), 1)

    def test_retry_covers_trailing_read_after_the_verifier_returns(self):
        verifier = Mock(return_value={"fixture": True})
        original = storage_module._Reader.read
        failed = False

        def read(reader, size):
            nonlocal failed
            if not failed:
                failed = True
                raise OSError(errno.EIO, "temporary trailing read failure")
            return original(reader, size)

        def retry(callback):
            with self.assertRaises(OSError):
                callback()
            return callback()

        with patch.object(storage_module._Reader, "read", read):
            receipt = self.storage.write_file(
                "test",
                "major-test",
                self.producer,
                verifier=verifier,
                readback_retry=retry,
            )
        self.assertTrue(receipt["verified"])
        self.assertEqual(self.producer.call_count, 1)
        self.assertEqual(verifier.call_count, 2)

    def test_replaced_partial_is_not_published_by_retry(self):
        verifier = Mock(side_effect=OSError(errno.ETIMEDOUT, "temporary read"))

        def retry(callback):
            with self.assertRaises(OSError):
                callback()
            partial = next(self.destination.rglob("*.partial"))
            partial.rename(partial.with_suffix(".saved"))
            partial.write_bytes(self.payload)
            partial.chmod(0o600)
            return callback()

        with self.assertRaisesRegex(BackupStorageError, "identity changed"):
            self.storage.write_file(
                "test",
                "major-test",
                self.producer,
                verifier=verifier,
                readback_retry=retry,
            )
        self.assertEqual(verifier.call_count, 1)
        self.assertFalse(list(self.destination.rglob("*.tar")))

    def test_changed_bytes_still_fail_complete_hash_verification(self):
        verifier = Mock(
            side_effect=[OSError(errno.ETIMEDOUT, "temporary read"), {"fixture": True}]
        )

        def retry(callback):
            with self.assertRaises(OSError):
                callback()
            partial = next(self.destination.rglob("*.partial"))
            partial.write_bytes(b"x" * len(self.payload))
            return callback()

        with self.assertRaisesRegex(BackupStorageError, "complete bytes and identity"):
            self.storage.write_file(
                "test",
                "major-test",
                self.producer,
                verifier=verifier,
                readback_retry=retry,
            )
        self.assertFalse(list(self.destination.rglob("*.tar")))
        self.assertFalse(list(self.destination.rglob("*.partial")))

    def test_retry_hook_cannot_skip_the_complete_readback(self):
        with self.assertRaisesRegex(BackupStorageError, "no completed readback"):
            self.storage.write_file(
                "test",
                "major-test",
                self.producer,
                readback_retry=lambda callback: {"fixture": True},
            )
        self.assertFalse(list(self.destination.rglob("*.tar")))
        self.assertFalse(list(self.destination.rglob("*.partial")))

    def test_transfer_retry_removes_its_failed_partial_before_repeating_producer(self):
        attempts = 0

        def produce(handle):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                handle.write(self.payload[:59])
                raise OSError(errno.EIO, "temporary transfer failure")
            handle.write(self.payload)

        def retry(callback):
            with self.assertRaises(OSError) as failure:
                callback()
            self.assertIsNot(
                getattr(failure.exception, "stage_retry_safe", None), False
            )
            self.assertFalse(list(self.destination.rglob("*.partial")))
            self.assertFalse(list(self.destination.rglob("*.tar")))
            return callback()

        receipt = self.storage.write_file(
            "test", "major-test", produce, write_retry=retry
        )
        self.assertEqual(attempts, 2)
        self.assertEqual(receipt["sha256"], hashlib.sha256(self.payload).hexdigest())
        self.assertEqual(len(list(self.destination.rglob("*.tar"))), 1)
        self.assertFalse(list(self.destination.rglob("*.partial")))

    def test_transfer_cleanup_uncertainty_blocks_retry(self):
        def produce(handle):
            handle.write(self.payload[:59])
            raise OSError(errno.EIO, "temporary transfer failure")

        original = os.fsync

        def fsync(descriptor):
            if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise OSError(errno.EIO, "directory cleanup could not be persisted")
            return original(descriptor)

        def retry(callback):
            with self.assertRaises(OSError) as failure:
                callback()
            self.assertIs(failure.exception.stage_retry_safe, False)
            raise failure.exception

        producer = Mock(side_effect=produce)
        with (
            patch.object(storage_module.os, "fsync", fsync),
            self.assertRaises(OSError),
        ):
            self.storage.write_file("test", "major-test", producer, write_retry=retry)
        self.assertEqual(producer.call_count, 1)
        self.assertFalse(list(self.destination.rglob("*.tar")))

    def test_replaced_failed_partial_is_preserved_and_cannot_retry(self):
        replacement = None

        def produce(handle):
            nonlocal replacement
            handle.write(self.payload[:59])
            replacement = next(self.destination.rglob("*.partial"))
            replacement.rename(replacement.with_suffix(".saved"))
            replacement.write_bytes(b"unowned replacement")
            replacement.chmod(0o600)
            raise OSError(errno.EIO, "temporary transfer failure")

        def retry(callback):
            with self.assertRaises(OSError) as failure:
                callback()
            self.assertIs(failure.exception.stage_retry_safe, False)
            raise failure.exception

        with self.assertRaises(OSError):
            self.storage.write_file("test", "major-test", produce, write_retry=retry)
        self.assertEqual(replacement.read_bytes(), b"unowned replacement")
        self.assertFalse(list(self.destination.rglob("*.tar")))

    def test_publication_fsync_failure_never_repeats_a_completed_transfer(self):
        original = os.fsync

        def fsync(descriptor):
            if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise OSError(errno.EIO, "archive publication is ambiguous")
            return original(descriptor)

        retry = Mock(side_effect=lambda callback: callback())
        with (
            patch.object(storage_module.os, "fsync", fsync),
            self.assertRaises(OSError),
        ):
            self.storage.write_file(
                "test", "major-test", self.producer, write_retry=retry
            )
        self.assertEqual(self.producer.call_count, 1)
        self.assertEqual(retry.call_count, 1)
        self.assertEqual(len(list(self.destination.rglob("*.tar"))), 1)
        self.assertFalse(list(self.destination.rglob("*.partial")))

    def test_transfer_hook_cannot_skip_writing_the_archive(self):
        with self.assertRaisesRegex(BackupStorageError, "no completed transfer"):
            self.storage.write_file(
                "test",
                "major-test",
                self.producer,
                write_retry=lambda callback: {"fixture": True},
            )
        self.producer.assert_not_called()
        self.assertFalse(list(self.destination.rglob("*.tar")))
        self.assertFalse(list(self.destination.rglob("*.partial")))

    def test_real_migration_archive_retries_parser_without_recopying_source(self):
        source, runtime, scratch = (
            self.root / "source",
            self.root / "runtime",
            self.root / "scratch",
        )
        for directory in (source, runtime, scratch):
            directory.mkdir(mode=0o700)
        (source / "blob").write_bytes(self.payload)
        (runtime / "application").write_bytes(b"retained runtime")
        config = scratch / "dumb-config.json"
        config.write_text('{"fixture":true}')
        config.chmod(0o600)

        def identity(directory):
            value = directory.stat()
            return {
                "device": value.st_dev,
                "inode": value.st_ino,
                "mode": stat.S_IFMT(value.st_mode),
            }

        plan = {
            "source": {
                "data_dir": str(source),
                "runtime_dir": str(runtime),
                "data_identity": identity(source),
                "runtime_identity": {"directory": identity(runtime)},
            },
            "backup_storage": {"destination_id": "test"},
        }
        fingerprints = {
            str(directory): tree_fingerprint(directory, scratch_root=scratch)
            for directory in (source, runtime)
        }
        original = archive_module._verify_stream
        attempts = 0

        def verify(handle, *args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                self.assertTrue(handle.read(57))
                raise OSError(errno.ETIMEDOUT, "temporary archive read failure")
            return original(handle, *args, **kwargs)

        def retry(callback):
            with self.assertRaises(OSError):
                callback()
            return callback()

        with (
            patch.object(archive_module, "_verify_stream", side_effect=verify),
            patch.object(
                archive_module, "iter_tree", wraps=archive_module.iter_tree
            ) as source_walks,
        ):
            result = archive_module.create_retained_archive(
                plan,
                {"root": str(scratch), "config_backup": str(config)},
                self.storage,
                fingerprints,
                readback_retry=retry,
            )
        self.assertEqual(attempts, 2)
        self.assertEqual(source_walks.call_count, 2)
        self.assertTrue(result["receipt"]["verified"])
        self.assertEqual(len(list(self.destination.rglob("*.tar"))), 1)
        self.assertFalse(list(self.destination.rglob("*.partial")))

    def test_real_archive_transfer_retry_keeps_prior_fingerprints_and_postgres_dump(
        self,
    ):
        source, runtime, scratch = (
            self.root / "source",
            self.root / "runtime",
            self.root / "scratch",
        )
        for directory in (source, runtime, scratch):
            directory.mkdir(mode=0o700)
        (source / "blob").write_bytes(self.payload)
        (runtime / "application").write_bytes(b"retained runtime")
        config, dump = scratch / "dumb-config.json", scratch / "v1-postgres.dump"
        config.write_text('{"fixture":true}')
        dump.write_bytes(b"already captured and independently verified PostgreSQL dump")
        for path in (config, dump):
            path.chmod(0o600)

        def identity(directory):
            value = directory.stat()
            return {
                "device": value.st_dev,
                "inode": value.st_ino,
                "mode": stat.S_IFMT(value.st_mode),
            }

        plan = {
            "source": {
                "data_dir": str(source),
                "runtime_dir": str(runtime),
                "data_identity": identity(source),
                "runtime_identity": {"directory": identity(runtime)},
            },
            "backup_storage": {"destination_id": "test"},
        }
        fingerprints = {
            str(directory): tree_fingerprint(directory, scratch_root=scratch)
            for directory in (source, runtime)
        }
        dump_before = dump.read_bytes(), dump.stat()
        original_write = storage_module._Writer.write
        failed = False

        def write(writer, data):
            nonlocal failed
            if not failed:
                failed = True
                original_write(writer, data[:61])
                raise OSError(errno.ETIMEDOUT, "temporary archive transfer failure")
            return original_write(writer, data)

        def retry(callback):
            with self.assertRaises(OSError) as failure:
                callback()
            self.assertIsNot(
                getattr(failure.exception, "stage_retry_safe", None), False
            )
            self.assertFalse(list(self.destination.rglob("*.partial")))
            return callback()

        with (
            patch.object(storage_module._Writer, "write", write),
            patch.object(
                archive_module, "iter_tree", wraps=archive_module.iter_tree
            ) as source_walks,
            patch.object(
                archive_module, "_verify_stream", wraps=archive_module._verify_stream
            ) as readbacks,
        ):
            result = archive_module.create_retained_archive(
                plan,
                {"root": str(scratch), "config_backup": str(config)},
                self.storage,
                fingerprints,
                extra_files={"v1-postgres.dump": dump},
                write_retry=retry,
            )
        self.assertTrue(result["receipt"]["verified"])
        self.assertEqual(readbacks.call_count, 1)
        self.assertEqual(source_walks.call_count, 4)
        self.assertEqual(dump.read_bytes(), dump_before[0])
        self.assertEqual(dump.stat().st_ino, dump_before[1].st_ino)
        self.assertEqual(dump.stat().st_mtime_ns, dump_before[1].st_mtime_ns)
        self.assertEqual(
            fingerprints,
            {
                str(directory): tree_fingerprint(directory, scratch_root=scratch)
                for directory in (source, runtime)
            },
        )
        self.assertEqual(len(list(self.destination.rglob("*.tar"))), 1)
        self.assertFalse(list(self.destination.rglob("*.partial")))


if __name__ == "__main__":
    unittest.main()
