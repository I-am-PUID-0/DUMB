import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from utils.infinidysk_v2_restore import verify_postgres_restore


class RestoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.dump = self.root / "backup.dump"
        self.dump.write_bytes(b"fixture custom dump")
        self.binding = {
            "binding_fingerprint": "a" * 64,
            "physical_identity": {"system_identifier": "111", "database_oid": "11"},
        }
        self.proof = {
            "verified": True,
            "source_binding": "a" * 64,
            "tables_verified": 8,
            "restored_identity": {"system_identifier": "222", "database_oid": "22"},
        }
        self.process = Mock(pid=987654, returncode=0, stdout=None, stderr=None)
        self.process.poll.return_value = 0
        self.process.communicate.return_value = (json.dumps(self.proof), "")
        self.patches = [
            patch("utils.infinidysk_v2_restore.os.geteuid", return_value=0),
            patch(
                "utils.infinidysk_v2_restore.postgres_tools",
                return_value=Path("/private/tools"),
            ),
            patch(
                "utils.infinidysk_v2_restore.namespace_command",
                side_effect=lambda uid, gid, cmd: cmd,
            ),
            patch(
                "utils.infinidysk_v2_restore.subprocess.Popen",
                return_value=self.process,
            ),
            patch("utils.infinidysk_v2_restore.os.killpg"),
        ]
        for patcher in self.patches:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patches):
            patcher.stop()
        self.temp.cleanup()

    def test_real_restore_proof_is_bound_to_an_independent_target_and_drops_source_credentials(
        self,
    ):
        payloads = []

        def communicate(**kwargs):
            import utils.infinidysk_v2_restore as module

            command = module.subprocess.Popen.call_args.args[0]
            payload = json.loads(Path(command[-1]).read_text())
            payloads.append(payload)
            self.assertNotIn("source-private-password", json.dumps(payload))
            self.assertEqual((payload["uid"], payload["gid"]), (1000, 1000))
            return json.dumps(self.proof), ""

        self.process.communicate.side_effect = communicate
        result = verify_postgres_restore(
            str(self.dump), self.binding, {"password": "source-private-password"}
        )
        self.assertEqual(result, self.proof)
        self.assertTrue(payloads)
        self.assertFalse(Path(payloads[0]["root"]).exists())

    def test_wrong_binding_original_identity_or_missing_verification_is_rejected(self):
        for change in (
            {"source_binding": "other"},
            {"restored_identity": self.binding["physical_identity"]},
            {"restored_identity": None},
            {"verified": False},
        ):
            with self.subTest(change=change):
                self.process.communicate.return_value = (
                    json.dumps({**self.proof, **change}),
                    "",
                )
                with self.assertRaisesRegex(ValueError, "binding"):
                    verify_postgres_restore(str(self.dump), self.binding, {})

    def test_timeout_terminates_owned_process_group_and_reaps_worker(self):
        self.process.communicate.side_effect = [
            subprocess.TimeoutExpired("private-helper", 300),
            ("", ""),
        ]
        with self.assertRaisesRegex(ValueError, "bound"):
            verify_postgres_restore(str(self.dump), self.binding, {})
        import utils.infinidysk_v2_restore as module

        self.assertGreaterEqual(module.os.killpg.call_count, 1)

    def test_redirected_dump_is_rejected_before_spawning(self):
        link = self.root / "link.dump"
        link.symlink_to(self.dump)
        with self.assertRaisesRegex(ValueError, "direct"):
            verify_postgres_restore(str(link), self.binding, {})
        import utils.infinidysk_v2_restore as module

        module.subprocess.Popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
