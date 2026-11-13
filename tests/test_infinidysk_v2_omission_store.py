"""Private proof sidecars preserve bindings, ownership, bounds and ancestry."""

import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests.test_infinidysk_v2_omissions import sample_proof
from utils import infinidysk_v2_omission_store as store
from utils.infinidysk_v2_omissions import OmissionProofError, make_omission_proof


class OmissionStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "owned-job"
        self.root.mkdir(mode=0o700)
        self.path = self.root / "omission-proof.json"
        self.job = "1" * 32
        self.plan = "a" * 64
        self.proof = sample_proof()

    def write(self):
        return store.write_proof(self.root, self.job, self.plan, self.proof)

    def read(self, ref):
        return store.read_proof(self.root, self.job, self.plan, ref)

    def test_roundtrip_keeps_only_small_ref_and_private_regular_sidecar(self):
        ref = self.write()
        self.assertEqual(self.read(ref), self.proof)
        self.assertEqual(set(ref), {"sha256", "size", "identity"})
        self.assertLess(len(json.dumps(ref)), 256)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(self.path.stat().st_uid, os.geteuid())
        self.assertEqual(self.path.stat().st_nlink, 1)
        self.assertEqual(ref["size"], self.path.stat().st_size)
        self.assertNotIn("omitted", ref)

    def test_same_write_is_idempotent_but_different_proof_cannot_replace_review(self):
        ref = self.write()
        before = self.path.read_bytes()
        self.assertEqual(self.write(), ref)
        changed = make_omission_proof(
            self.proof["source_media"],
            self.proof["source_history"],
            self.proof["omitted"][:1],
        )
        with self.assertRaisesRegex(
            OmissionProofError, "different private omission proof"
        ):
            store.write_proof(self.root, self.job, self.plan, changed)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.read(ref), self.proof)

    def test_wrong_job_plan_digest_size_or_identity_reference_is_rejected(self):
        ref = self.write()
        for job, plan, value in (
            ("2" * 32, self.plan, ref),
            (self.job, "b" * 64, ref),
            (self.job, self.plan, ref | {"sha256": "0" * 64}),
            (self.job, self.plan, ref | {"size": ref["size"] + 1}),
            (self.job, self.plan, ref | {"identity": [0, 0]}),
            (self.job, self.plan, ref | {"unexpected": "reserved-secret"}),
        ):
            with self.subTest(job=job, plan=plan, ref_keys=sorted(value)):
                with self.assertRaises(OmissionProofError) as caught:
                    store.read_proof(self.root, job, plan, value)
                self.assertNotIn("reserved-secret", str(caught.exception))

    def test_copy_with_identical_bytes_does_not_satisfy_original_file_identity(self):
        ref = self.write()
        replacement = self.root / "replacement"
        replacement.write_bytes(self.path.read_bytes())
        replacement.chmod(0o600)
        replacement.replace(self.path)
        with self.assertRaises(OmissionProofError):
            self.read(ref)

    def test_leaf_symlink_or_hardlink_is_rejected_and_preserved(self):
        ref = self.write()
        original = self.path.read_bytes()
        outside = self.base / "outside"
        self.path.rename(outside)
        self.path.symlink_to(outside)
        for operation in (self.write, lambda: self.read(ref)):
            with self.assertRaises(OmissionProofError):
                operation()
        self.assertTrue(self.path.is_symlink())
        self.assertEqual(outside.read_bytes(), original)
        self.path.unlink()
        os.link(outside, self.path)
        for operation in (self.write, lambda: self.read(ref)):
            with self.assertRaises(OmissionProofError):
                operation()
        self.assertEqual(self.path.stat().st_nlink, 2)

    def test_shared_root_or_leaf_modes_are_not_changed_automatically(self):
        ref = self.write()
        self.path.chmod(0o640)
        with self.assertRaises(OmissionProofError):
            self.read(ref)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o640)
        self.path.chmod(0o600)
        self.root.chmod(0o750)
        with self.assertRaises(OmissionProofError):
            self.read(ref)
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o750)

    def test_foreign_leaf_owner_is_rejected_before_reading_contents(self):
        ref = self.write()
        original = store.os.fstat

        def foreign(descriptor):
            value = original(descriptor)
            if stat.S_ISREG(value.st_mode):
                fields = {
                    field: getattr(value, field)
                    for field in (
                        "st_dev",
                        "st_ino",
                        "st_size",
                        "st_mtime_ns",
                        "st_ctime_ns",
                        "st_mode",
                        "st_uid",
                        "st_gid",
                        "st_nlink",
                    )
                }
                fields["st_uid"] = os.geteuid() + 1
                return SimpleNamespace(**fields)
            return value

        with patch.object(store.os, "fstat", foreign):
            with self.assertRaises(OmissionProofError):
                self.read(ref)

    def test_fifo_leaf_is_rejected_without_waiting_for_a_writer(self):
        os.mkfifo(self.path)
        with self.assertRaises(OmissionProofError):
            self.write()
        self.assertTrue(stat.S_ISFIFO(self.path.lstat().st_mode))

    def test_symlinked_root_or_ancestor_never_receives_proof(self):
        outside = self.base / "outside"
        outside.mkdir(mode=0o700)
        self.root.rmdir()
        self.root.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(OmissionProofError):
            self.write()
        self.assertFalse((outside / "omission-proof.json").exists())
        self.root.unlink()
        alias = self.base / "parent-link"
        alias.symlink_to(outside, target_is_directory=True)
        (outside / "owned-job").mkdir(mode=0o700)
        with self.assertRaises(OmissionProofError):
            store.write_proof(alias / "owned-job", self.job, self.plan, self.proof)
        self.assertFalse((outside / "owned-job" / "omission-proof.json").exists())

    def test_ancestor_swap_during_write_cannot_redirect_private_bytes(self):
        original = store.atomic_write_private_text
        retained = self.base / "retained-root"
        outside = self.base / "outside"
        outside.mkdir(mode=0o700)

        def swapped(*args, **kwargs):
            self.root.rename(retained)
            self.root.symlink_to(outside, target_is_directory=True)
            return original(*args, **kwargs)

        with patch.object(store, "atomic_write_private_text", swapped):
            with self.assertRaises(OmissionProofError):
                self.write()
        self.assertFalse((outside / "omission-proof.json").exists())
        self.assertTrue((retained / "omission-proof.json").is_file())
        self.assertEqual(
            stat.S_IMODE((retained / "omission-proof.json").stat().st_mode), 0o600
        )

    def test_ancestor_swap_during_read_refuses_returning_the_proof(self):
        ref = self.write()
        original = store._decode
        retained = self.base / "retained-root"

        def swapped(*args, **kwargs):
            result = original(*args, **kwargs)
            self.root.rename(retained)
            self.root.mkdir(mode=0o700)
            return result

        with patch.object(store, "_decode", swapped):
            with self.assertRaises(OmissionProofError):
                self.read(ref)
        self.assertTrue((retained / "omission-proof.json").is_file())

    def test_file_change_during_read_is_refused_before_decoding(self):
        ref = self.write()
        original = store.os.fstat
        calls = []

        def changed(descriptor):
            value = original(descriptor)
            if stat.S_ISREG(value.st_mode):
                calls.append(descriptor)
                if len(calls) == 2:
                    self.path.write_bytes(self.path.read_bytes() + b" ")
                    return original(descriptor)
            return value

        with patch.object(store.os, "fstat", changed):
            with self.assertRaises(OmissionProofError):
                self.read(ref)

    def test_bounded_envelope_failure_creates_no_partial_or_proof_file(self):
        with patch.object(store, "MAX_OMISSION_PROOF_BYTES", 100):
            with self.assertRaisesRegex(OmissionProofError, "storage bound"):
                self.write()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_header_binding_is_checked_even_with_matching_modified_reference(self):
        ref = self.write()
        envelope = json.loads(self.path.read_bytes())
        envelope["job_id"] = "2" * 32
        raw = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()
        self.path.write_bytes(raw)
        changed = ref | {"sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw)}
        with self.assertRaises(OmissionProofError):
            self.read(changed)

    def test_missing_or_noncanonical_json_is_not_repaired(self):
        with self.assertRaises(OmissionProofError):
            self.read({"sha256": "a" * 64, "size": 100, "identity": [0, 0]})
        ref = self.write()
        raw = self.path.read_bytes() + b"\n"
        self.path.write_bytes(raw)
        changed = ref | {"sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw)}
        with self.assertRaises(OmissionProofError):
            self.read(changed)


if __name__ == "__main__":
    unittest.main()
