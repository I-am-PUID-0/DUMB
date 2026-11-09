import copy
from pathlib import Path
import tempfile
import unittest

from utils.infinidysk_v2_support import (
    api_version_supported,
    validate_candidate,
    versioned_capabilities,
)


class NativeSupportPolicyTests(unittest.TestCase):
    def test_semantic_floor_and_unknown_versions(self):
        for value in (
            None,
            "",
            "2.9.0",
            "2.23.99",
            "2.24",
            "2.24.0-dev.1",
            "arbitrary",
            "02.24.0",
            "2.024.0",
            "2.24.0+build..1",
            "v2.24.1",
        ):
            self.assertFalse(api_version_supported(value), value)
        for value in (
            "2.24.0",
            "2.24.1",
            "2.100.0",
            "3.0.0",
            "2.24.0+build.1",
            "2.25.0-dev.1",
        ):
            self.assertTrue(api_version_supported(value), value)
        self.assertFalse(versioned_capabilities("2.23.0")["infinidysk_v2_migration"])
        self.assertEqual(
            1, versioned_capabilities("2.24.0")["infinidysk_v2_migration_protocol"]
        )

    def test_raw_generation_switch_preserves_even_empty_existing_database(self):
        with tempfile.TemporaryDirectory() as scratch:
            current = {"runtime_generation": "1", "config_dir": scratch}
            target = {
                **current,
                "runtime_generation": "2",
                "v2_data_dir": str(Path(scratch, "separate")),
            }
            self.assertTrue(validate_candidate(current, target)[0])
            Path(scratch, "db.sqlite").touch()
            self.assertFalse(validate_candidate(current, target)[0])
            Path(scratch, "db.sqlite").unlink()
            current["postgres_enabled"] = True
            self.assertFalse(validate_candidate(current, target)[0])

    def test_native_mount_and_provider_edits_need_their_owned_workflows(self):
        service = {
            "runtime_generation": "2",
            "mount_backend": "rclone",
            "mount_path": "/mnt/example",
            "v2_data_dir": "/data/example-v2",
        }
        self.assertTrue(validate_candidate(service, copy.deepcopy(service))[0])
        for change in (
            {"runtime_generation": "1"},
            {"mount_backend": "fuse"},
            {"postgres_enabled": True},
            {"mount_path": "/mnt/other"},
            {"env": {"INFINIDYSK_DATABASE_URL": "postgres://example.invalid/new"}},
        ):
            self.assertFalse(
                validate_candidate(service, {**service, **change})[0], change
            )


if __name__ == "__main__":
    unittest.main()
