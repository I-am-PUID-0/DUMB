"""The normal version fields survive conversion, reload and user pin changes."""

import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests.test_env_example import ROOT, _load_config_manager_class
from utils.infinidysk_release_controls import (
    normalize_config,
    normalized_service,
    selected_release,
    set_release_selection,
)
from utils.infinidysk_v2_support import validate_candidate


class NativeReleaseControlsTests(unittest.TestCase):
    def test_standard_controls_pin_and_return_to_latest(self):
        service = {
            "runtime_generation": "2",
            "release_version": "rc",
            "release_version_enabled": False,
            "auto_update": False,
            "auto_update_mode": "check_only",
        }
        self.assertEqual(selected_release(service), "latest")
        set_release_selection(service, "v2.0.0-beta.10")
        self.assertTrue(service["release_version_enabled"])
        self.assertEqual(selected_release(service), "v2.0.0-beta.10")
        set_release_selection(service, "latest")
        self.assertFalse(service["release_version_enabled"])
        self.assertEqual(selected_release(service), "latest")
        self.assertFalse(service["auto_update"])
        self.assertEqual(service["auto_update_mode"], "check_only")

    def test_legacy_effective_choice_wins_over_dormant_v1_fields(self):
        for selected, enabled in (("latest", False), ("v2.0.0-beta.10", True)):
            with self.subTest(selected=selected):
                legacy = {
                    "runtime_generation": "2",
                    "v2_release_version": selected,
                    "release_version": "rc",
                    "release_version_enabled": True,
                }
                original = copy.deepcopy(legacy)
                converted = normalized_service(legacy)
                self.assertEqual(legacy, original)
                self.assertNotIn("v2_release_version", converted)
                self.assertEqual(converted["release_version"], selected)
                self.assertEqual(converted["release_version_enabled"], enabled)
                self.assertEqual(selected_release(converted), selected)

    def test_v1_controls_and_unrelated_payloads_remain_unchanged(self):
        legacy = {
            "runtime_generation": "1",
            "v2_release_version": "v2.0.0-beta.6",
            "release_version": "rc",
            "release_version_enabled": True,
            "branch_enabled": False,
        }
        expected = {
            key: value for key, value in legacy.items() if key != "v2_release_version"
        }
        config = {"infinidysk": legacy, "other": {"v2_release_version": "owned"}}
        normalize_config(config)
        self.assertEqual(config["infinidysk"], expected)
        self.assertEqual(config["other"], {"v2_release_version": "owned"})

    def test_native_api_does_not_reintroduce_the_retired_key(self):
        current = {
            "runtime_generation": "2",
            "release_version": "latest",
            "release_version_enabled": False,
        }
        candidate = {**current, "v2_release_version": "v2.0.0-beta.10"}
        valid, reason = validate_candidate(current, candidate)
        self.assertFalse(valid)
        self.assertIn("release_version_enabled", reason)

    def test_bad_selector_types_fail_without_replacing_configuration(self):
        for value in (False, 0, [], {}):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    selected_release({"v2_release_version": value})
        with self.assertRaises(ValueError):
            selected_release({"release_version_enabled": "true"})

    def test_actual_loader_converts_once_and_retains_original_config_backup(self):
        manager_class = _load_config_manager_class()
        for generation in ("1", "2"):
            for selected in ("latest", "v2.0.0-beta.10"):
                with self.subTest(generation=generation, selected=selected):
                    with tempfile.TemporaryDirectory() as temporary:
                        root = Path(temporary)
                        config = json.loads(
                            (ROOT / "utils" / "dumb_config.json").read_text()
                        )
                        service = config["infinidysk"]
                        service.update(
                            runtime_generation=generation,
                            v2_release_version=selected,
                            release_version="rc",
                            release_version_enabled=True,
                            auto_update=False,
                            auto_update_mode="check_only",
                        )
                        path = root / "dumb_config.json"
                        original = json.dumps(config).encode()
                        path.write_bytes(original)
                        record = root / "migrations" / "retained.json"
                        record.parent.mkdir()
                        record.write_bytes(b'{"retained":"unchanged"}')
                        with patch.dict(os.environ, {}, clear=True):
                            loaded = manager_class(
                                str(path),
                                str(ROOT / "utils" / "dumb_config_schema.json"),
                            )
                        converted = loaded.config["infinidysk"]
                        self.assertNotIn("v2_release_version", converted)
                        if generation == "2":
                            self.assertEqual(selected_release(converted), selected)
                            self.assertEqual(
                                converted["release_version_enabled"],
                                selected != "latest",
                            )
                        else:
                            self.assertEqual(converted["release_version"], "rc")
                            self.assertTrue(converted["release_version_enabled"])
                        self.assertFalse(converted["auto_update"])
                        self.assertEqual(converted["auto_update_mode"], "check_only")
                        self.assertEqual(
                            Path(str(path) + ".bak").read_bytes(), original
                        )
                        self.assertEqual(
                            record.read_bytes(), b'{"retained":"unchanged"}'
                        )
                        saved = path.read_bytes()
                        with patch.dict(os.environ, {}, clear=True):
                            again = manager_class(
                                str(path),
                                str(ROOT / "utils" / "dumb_config_schema.json"),
                            )
                        self.assertEqual(again.config["infinidysk"], converted)
                        self.assertEqual(path.read_bytes(), saved)
                        self.assertEqual(
                            Path(str(path) + ".bak").read_bytes(), original
                        )


if __name__ == "__main__":
    unittest.main()
