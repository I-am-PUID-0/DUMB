"""Service selections never acquire another instance's state or DUMB settings."""

from copy import deepcopy
from functools import wraps
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from utils.service_backup_scope import (
    ServiceBackupScopeError,
    affected_running_rows,
    configured_service_entries,
    enumerate_service_choices,
    merge_service_config,
    resolve_service_scope,
    service_config_fragment,
)


def isolated_dependency_test(method):
    """Keep real dependency contracts outside legacy global router doubles."""

    @wraps(method)
    def isolated(self):
        identifier = (
            f"tests.{Path(__file__).stem}.{type(self).__name__}.{method.__name__}"
        )
        if os.environ.get("DUMB_SERVICE_BACKUP_DEPENDENCY_TEST_ID") == identifier:
            return method(self)
        result = subprocess.run(
            [sys.executable, "-B", "-m", "unittest", identifier],
            cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, "DUMB_SERVICE_BACKUP_DEPENDENCY_TEST_ID": identifier},
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    return isolated


class ServiceBackupFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config_root = self.root / "controller"
        self.config_root.mkdir()
        self.config_path = self.config_root / "dumb.json"
        self.config_path.write_text("{}")
        self.state_root = self.config_root / "installation-backups"
        self.data_root = self.root / "data"
        self.data_root.mkdir()
        self.first = self.root / "first"
        self.second = self.root / "second"
        self.first.mkdir()
        self.second.mkdir()
        self.config = {
            "puid": 1000,
            "pgid": 1000,
            "data_root": str(self.data_root),
            "dumb": {
                "api_service": {"process_name": "DUMB API"},
                "onboarding_completed": True,
            },
            "backup_storage": {"profiles": {}},
            "fixture": {
                "instances": {
                    "first": {
                        "process_name": "First service",
                        "enabled": False,
                        "config_dir": str(self.first),
                        "env": {"PASSWORD": "fixture-only"},
                    },
                    "second": {
                        "process_name": "Second service",
                        "enabled": True,
                        "config_dir": str(self.second),
                        "env": {},
                    },
                }
            },
            "other": {
                "process_name": "Other service",
                "enabled": False,
                "config_dir": str(self.root / "other"),
            },
        }
        patcher = patch("utils.backup_storage.mount_records", return_value=[])
        patcher.start()
        self.addCleanup(patcher.stop)
        environment = patch.dict(
            os.environ, {"DUMB_AUTH_CONFIG_PATH": str(self.config_root / "users.json")}
        )
        environment.start()
        self.addCleanup(environment.stop)

    def scope(self, identifiers=None, **kwargs):
        return resolve_service_scope(
            self.config,
            identifiers or ["fixture:first"],
            self.config_path,
            self.state_root,
            **kwargs,
        )

    def first_config(self):
        return self.config["fixture"]["instances"]["first"]

    def paths(self, scope=None):
        return {row["path"]: row for row in (scope or self.scope())["roots"]}


class ServiceBackupScopeTests(ServiceBackupFixture):
    def test_exact_configured_instance_identity_includes_disabled_data(self):
        scope = self.scope()
        self.assertEqual(scope["service_ids"], ["fixture:first"])
        self.assertEqual(
            scope["services"],
            [
                {
                    "id": "fixture:first",
                    "key": "fixture",
                    "instance_name": "first",
                    "name": "First service",
                    "enabled": False,
                }
            ],
        )
        self.assertEqual(set(self.paths(scope)), {str(self.first)})
        self.assertNotIn("service_config", scope["services"][0])
        self.assertEqual(
            scope["configuration"],
            {"fixture": {"instances": {"first": self.first_config()}}},
        )
        self.assertEqual(scope["exclusions"], [])

    def test_display_names_are_not_selectors_or_service_identity(self):
        self.config["fixture"]["instances"]["second"]["process_name"] = "First service"
        self.assertEqual(
            [row["id"] for row in configured_service_entries(self.config)],
            ["fixture:first", "fixture:second", "other"],
        )
        with self.assertRaisesRegex(ServiceBackupScopeError, "no longer exists"):
            self.scope(["First service"])

    def test_nested_control_names_cannot_be_selected_as_application_processes(self):
        for child in ("api_service", "frontend"):
            self.config["dumb"][child] = {"process_name": "Reserved controller process"}
            self.first_config()["process_name"] = "Reserved controller process"
            with (
                self.subTest(child=child),
                self.assertRaisesRegex(ServiceBackupScopeError, "shared or reserved"),
            ):
                self.scope()
            choices = enumerate_service_choices(
                self.config, self.config_path, self.state_root
            )
            choice = next(row for row in choices if row["id"] == "fixture:first")
            self.assertFalse(choice["supported"])
            self.assertIn("shared or reserved", choice["reason"])
            del self.config["dumb"][child]

    def test_duplicate_application_names_are_blocked_even_when_all_are_disabled(self):
        self.config["fixture"]["instances"]["second"].update(
            enabled=False, process_name="First service"
        )
        with self.assertRaisesRegex(ServiceBackupScopeError, "shared or reserved"):
            self.scope()
        with self.assertRaisesRegex(ServiceBackupScopeError, "shared or reserved"):
            self.scope(["fixture:first", "fixture:second"])

    def test_reserved_pgagent_name_is_blocked_without_enabled_parent_services(self):
        self.first_config()["process_name"] = "pgAgent"
        self.assertFalse(self.first_config()["enabled"])
        with self.assertRaisesRegex(ServiceBackupScopeError, "shared or reserved"):
            self.scope()
        self.config["postgres"] = {"process_name": "PostgreSQL", "enabled": False}
        self.config["pgadmin"] = {"process_name": "pgAdmin", "enabled": False}
        with self.assertRaisesRegex(ServiceBackupScopeError, "shared or reserved"):
            self.scope()

    def test_selection_rejects_duplicates_unknowns_and_non_list_inputs(self):
        for selection in (
            [],
            "fixture:first",
            ["fixture:first", "fixture:first"],
            [None],
            ["fixture"],
            ["fixture:First"],
        ):
            with (
                self.subTest(selection=selection),
                self.assertRaises(ServiceBackupScopeError),
            ):
                resolve_service_scope(
                    self.config, selection, self.config_path, self.state_root
                )

    def test_malformed_instance_names_are_never_ambiguous(self):
        for name in (
            "bad/name",
            "bad:name",
            "bad\\name",
            "bad\nname",
            " first",
            "..",
            "a" * 81,
        ):
            self.config["fixture"]["instances"][name] = self.first_config()
            with self.subTest(name=name), self.assertRaises(ServiceBackupScopeError):
                configured_service_entries(self.config)
            del self.config["fixture"]["instances"][name]

    def test_unicode_exact_instance_names_can_be_selected(self):
        self.config["fixture"]["instances"]["Series français"] = self.config["fixture"][
            "instances"
        ].pop("first")
        self.assertEqual(
            self.scope(["fixture:Series français"])["service_ids"],
            ["fixture:Series français"],
        )

    def test_public_choices_never_contain_native_secrets(self):
        choices = enumerate_service_choices(
            self.config, self.config_path, self.state_root
        )
        self.assertEqual(choices[0]["supported"], True)
        self.assertFalse(choices[0]["enabled"])
        self.assertNotIn("PASSWORD", json.dumps(choices))
        self.assertNotIn("fixture-only", json.dumps(choices))
        self.assertNotIn("service_config", json.dumps(choices))

    def test_shipped_disabled_services_remain_discoverable_without_native_processes(
        self,
    ):
        from utils.runtime_paths import default_config_file

        defaults = json.loads(Path(default_config_file()).read_text())
        choices = enumerate_service_choices(defaults, self.config_path, self.state_root)
        indexed = {row["id"]: row for row in choices}
        for identifier in (
            "infinidysk",
            "radarr:Default",
            "rclone:RealDebrid",
            "postgres",
        ):
            self.assertIn(identifier, indexed)
        self.assertTrue(indexed["infinidysk"]["supported"])
        self.assertTrue(indexed["rclone:RealDebrid"]["supported"])
        self.assertFalse(indexed["postgres"]["supported"])

    def test_shared_cluster_and_control_services_require_whole_installation(self):
        for key in ("postgres", "dumb_api_service"):
            self.config[key] = {"process_name": key, "config_dir": str(self.root / key)}
            with (
                self.subTest(key=key),
                self.assertRaisesRegex(ServiceBackupScopeError, "whole-installation"),
            ):
                self.scope([key])

    def test_missing_service_data_can_still_carry_selected_configuration(self):
        self.first.rmdir()
        scope = self.scope()
        self.assertEqual(scope["roots"], [])
        self.assertIn("fixture", scope["configuration"])

    def test_restore_hint_recovers_only_the_current_declared_missing_directory(self):
        self.first.rmdir()
        scope = self.scope(
            restore_roots=[
                {
                    "path": str(self.first),
                    "kind": "directory",
                    "roles": ["authentication"],
                    "service_ids": ["other"],
                }
            ]
        )
        self.assertEqual(set(self.paths(scope)), {str(self.first)})
        row = scope["roots"][0]
        self.assertIsNone(row["identity"])
        self.assertEqual(row["service_ids"], ["fixture:first"])
        self.assertEqual(row["roles"], ["fixture:first:config_dir"])
        self.assertEqual(row["kind"], "directory")
        self.assertFalse(self.first.exists())

    def test_restore_hint_recovers_a_missing_explicit_file_and_absent_parent(self):
        filename = self.root / "missing-parent" / "settings.json"
        self.first_config()["settings_file"] = str(filename)
        scope = self.scope(restore_roots=[{"path": str(filename), "kind": "file"}])
        row = self.paths(scope)[str(filename)]
        self.assertEqual(row["kind"], "file")
        self.assertIsNone(row["identity"])
        self.assertEqual(row["roles"], ["fixture:first:settings_file"])
        self.assertFalse(filename.parent.exists())

    def test_missing_shared_persistence_requires_all_current_owners_even_with_hint(
        self,
    ):
        self.first.rmdir()
        self.config["other"]["config_dir"] = str(self.first)
        with self.assertRaisesRegex(
            ServiceBackupScopeError, "unselected service other"
        ):
            self.scope(restore_roots=[{"path": str(self.first), "kind": "directory"}])

    def test_restore_cannot_adopt_an_archive_only_or_lost_alias_target(self):
        with self.assertRaisesRegex(ServiceBackupScopeError, "not currently declared"):
            self.scope(
                restore_roots=[
                    {"path": str(self.root / "archive-only"), "kind": "directory"}
                ]
            )
        alias = self.root / "declared-alias"
        alias.symlink_to(self.first, target_is_directory=True)
        self.first_config()["config_dir"] = str(alias)
        roots = self.scope()["roots"]
        alias.unlink()
        with self.assertRaisesRegex(ServiceBackupScopeError, "not currently declared"):
            self.scope(restore_roots=roots)

    def test_restore_hints_reject_ambiguous_types_and_unnormalized_paths(self):
        for hints in (
            None,
            {},
            [{"path": str(self.first), "kind": "socket"}],
            [{"path": str(self.first / ".." / "elsewhere"), "kind": "directory"}],
            [{"path": str(self.first), "kind": "directory"}] * 2,
        ):
            with self.subTest(hints=hints), self.assertRaises(ServiceBackupScopeError):
                self.scope(restore_roots=hints)

    def test_shared_nested_or_parent_paths_block_unselected_owners(self):
        for path in (self.first, self.first / "nested", self.root):
            self.config["other"]["config_dir"] = str(path)
            with (
                self.subTest(path=path),
                self.assertRaisesRegex(
                    ServiceBackupScopeError, "unselected service other"
                ),
            ):
                self.scope()

    def test_unselected_symlink_alias_and_selected_alias_cannot_hide_shared_ownership(
        self,
    ):
        alias = self.root / "alias"
        alias.symlink_to(self.first, target_is_directory=True)
        self.config["other"]["config_dir"] = str(alias)
        with self.assertRaisesRegex(
            ServiceBackupScopeError, "unselected service other"
        ):
            self.scope()
        self.config["other"]["config_dir"] = str(self.first)
        self.first_config()["config_dir"] = str(alias)
        with self.assertRaisesRegex(
            ServiceBackupScopeError, "unselected service other"
        ):
            self.scope()

    def test_selected_alias_carries_literal_link_and_exclusive_physical_data(self):
        alias = self.root / "alias"
        alias.symlink_to(self.first, target_is_directory=True)
        self.first_config()["config_dir"] = str(alias)
        paths = self.paths()
        self.assertEqual(set(paths), {str(alias), str(self.first)})
        self.assertEqual(paths[str(alias)]["kind"], "symlink")
        self.assertEqual(paths[str(self.first)]["kind"], "directory")

    def test_parent_component_alias_is_preserved_as_literal_link(self):
        subdir = self.first / "child"
        subdir.mkdir()
        alias = self.root / "alias"
        alias.symlink_to(self.first, target_is_directory=True)
        self.first_config()["config_dir"] = str(alias / "child")
        paths = self.paths()
        self.assertEqual(set(paths), {str(alias), str(subdir)})
        self.assertTrue(paths[str(alias)]["link_only"])

    def test_all_explicit_owners_can_be_selected_without_duplicate_roots(self):
        self.config["other"]["config_dir"] = str(self.first / "nested")
        (self.first / "nested").mkdir()
        scope = self.scope(["fixture:first", "other"])
        self.assertEqual(set(self.paths(scope)), {str(self.first)})
        self.assertEqual(
            set(scope["roots"][0]["service_ids"]), {"fixture:first", "other"}
        )

    def test_shared_choices_explain_required_bundle_without_disabling_selection(self):
        self.config["other"]["config_dir"] = str(self.first)
        choices = enumerate_service_choices(
            self.config, self.config_path, self.state_root
        )
        choice = next(row for row in choices if row["id"] == "fixture:first")
        self.assertTrue(choice["supported"])
        self.assertEqual(choice["required_service_ids"], ["other"])
        self.assertIn("selecting these services together", choice["warning"])
        with self.assertRaises(ServiceBackupScopeError):
            self.scope()

    def test_shared_choice_becomes_unsupported_when_bundle_includes_shared_cluster(
        self,
    ):
        self.config["postgres"] = {
            "process_name": "PostgreSQL",
            "config_dir": str(self.first),
        }
        choices = enumerate_service_choices(
            self.config, self.config_path, self.state_root
        )
        choice = next(row for row in choices if row["id"] == "fixture:first")
        self.assertFalse(choice["supported"])
        self.assertIn("whole-installation", choice["reason"])

    def test_shared_config_parent_is_not_owned_but_explicit_file_is(self):
        filename = self.config_root / "native.conf"
        filename.write_text("fixture native config")
        service = self.first_config()
        service["config_dir"] = str(self.config_root)
        service["config_file"] = str(filename)
        self.assertEqual(set(self.paths()), {str(filename)})
        service.pop("config_file")
        with self.assertRaisesRegex(
            ServiceBackupScopeError, "shared persistence directory"
        ):
            self.scope()

    def test_shared_config_file_requires_all_instance_owners(self):
        filename = self.config_root / "native.conf"
        filename.write_text("fixture")
        for service in self.config["fixture"]["instances"].values():
            service["config_dir"] = str(self.config_root)
            service["config_file"] = str(filename)
        with self.assertRaisesRegex(
            ServiceBackupScopeError, "unselected service fixture:second"
        ):
            self.scope()
        self.assertEqual(
            set(self.paths(self.scope(["fixture:first", "fixture:second"]))),
            {str(filename)},
        )

    def test_controller_auth_and_recovery_paths_are_never_service_owned(self):
        for path in (
            self.config_path,
            self.config_root / ".env",
            self.config_root / "dumb.json.bak",
            self.config_root / "users.json",
            self.state_root / "payload",
            self.config_root / ".dumb-setup",
            self.config_root / "migrations" / "infinidysk-v2",
            self.config_root / "update_notices.json",
        ):
            self.first_config()["config_dir"] = str(path)
            with (
                self.subTest(path=path),
                self.assertRaisesRegex(
                    ServiceBackupScopeError, "authentication or recovery"
                ),
            ):
                self.scope()

    def test_auth_alias_is_protected_by_its_physical_path(self):
        auth = self.first / "auth.json"
        auth.write_text("{}")
        alias = self.config_root / "users.json"
        alias.symlink_to(auth)
        with self.assertRaisesRegex(
            ServiceBackupScopeError, "authentication or recovery"
        ):
            self.scope()

    def test_shared_system_and_data_root_selectors_are_rejected(self):
        for path in ("/", "/data", "/mnt/debrid", str(self.data_root)):
            self.first_config()["config_dir"] = path
            with self.subTest(path=path), self.assertRaises(ServiceBackupScopeError):
                self.scope()

    def test_relative_traversal_and_nul_selectors_are_rejected(self):
        for path in ("relative", "/tmp/../fixture", "/tmp/fixture\x00"):
            self.first_config()["config_dir"] = path
            with self.subTest(path=path), self.assertRaises(ServiceBackupScopeError):
                self.scope()

    def test_backup_profiles_are_not_recursively_included_or_overwritten(self):
        for path in (self.first, self.first / "archives", self.root):
            self.config["backup_storage"]["profiles"] = {"fixture": {"path": str(path)}}
            with (
                self.subTest(path=path),
                self.assertRaisesRegex(ServiceBackupScopeError, "backup destination"),
            ):
                self.scope()

    def test_current_destination_is_rechecked_even_without_saved_profile(self):
        with self.assertRaisesRegex(ServiceBackupScopeError, "backup destination"):
            self.scope(storage={"path": str(self.first / "archives")})

    def test_nested_mount_is_not_copied_as_service_appdata(self):
        with patch(
            "utils.backup_storage.mount_records",
            return_value=[
                {"path": str(self.first / "media"), "device": "0:42", "root": "/"},
                {"path": "/", "device": "0:1", "root": "/"},
            ],
        ):
            with self.assertRaisesRegex(ServiceBackupScopeError, "nested mount"):
                self.scope()

    def test_bind_mount_alias_cannot_hide_an_unselected_owner(self):
        self.config["other"]["config_dir"] = str(self.second)
        records = [
            {"path": "/", "device": "0:1", "root": "/"},
            {"path": str(self.first), "device": "0:42", "root": "/shared"},
            {"path": str(self.second), "device": "0:42", "root": "/shared/child"},
        ]
        with patch("utils.backup_storage.mount_records", return_value=records):
            with self.assertRaisesRegex(ServiceBackupScopeError, "unselected service"):
                self.scope()

    def test_sidecar_native_sqlite_paths_have_same_exclusive_ownership(self):
        database = self.root / "native.sqlite"
        database.write_text("fixture database")
        (self.root / "native.sqlite-wal").write_text("fixture wal")
        with patch(
            "utils.service_backup_scope._effective_persistence",
            side_effect=lambda key, service, data_root: (
                ([], [str(database)])
                if service.get("process_name") == "First service"
                else ([], [])
            ),
        ):
            paths = self.paths()
            self.assertIn(str(database), paths)
            self.assertIn(str(database) + "-wal", paths)
            self.assertNotIn(str(database) + "-shm", paths)

    def test_link_library_is_link_only_and_does_not_claim_media_payload(self):
        library = self.root / "library"
        library.mkdir()
        self.first_config()["symlink_backup_roots"] = [str(library)]
        self.assertTrue(self.paths()[str(library)]["link_only"])

    def test_default_service_data_mapping_is_per_instance(self):
        self.config["sonarr"] = self.config.pop("fixture")
        owned = self.data_root / "sonarr" / "first"
        unrelated = self.data_root / "sonarr" / "second"
        owned.mkdir(parents=True)
        unrelated.mkdir()
        with patch(
            "utils.service_backup_scope._effective_persistence", return_value=([], [])
        ):
            paths = self.paths(self.scope(["sonarr:first"]))
        self.assertIn(str(owned), paths)
        self.assertNotIn(str(owned.parent), paths)
        self.assertNotIn(str(unrelated), paths)

    def test_rclone_provider_config_is_reference_not_owned_native_state(self):
        self.config["rclone"] = self.config.pop("fixture")
        provider = self.root / "provider.yml"
        provider.write_text("fixture provider")
        self.config["rclone"]["instances"]["first"]["zurg_config_file"] = str(provider)
        self.config["other"]["config_file"] = str(provider)
        paths = self.paths(self.scope(["rclone:first"]))
        self.assertNotIn(str(provider), paths)

    def test_saved_whisparr_profile_data_is_included(self):
        self.config["whisparr"] = self.config.pop("fixture")
        saved = self.root / "saved-profile"
        saved.mkdir()
        self.config["whisparr"]["instances"]["first"]["whisparr_profiles"] = {
            "v2": {"initialized": True, "config_dir": str(saved)}
        }
        with patch(
            "utils.service_backup_scope._effective_persistence", return_value=([], [])
        ):
            self.assertIn(str(saved), self.paths(self.scope(["whisparr:first"])))


class ServiceConfigMergeTests(ServiceBackupFixture):
    def fragment(self):
        fragment = service_config_fragment(self.config, ["fixture:first"])
        fragment["fixture"]["instances"]["first"]["enabled"] = True
        fragment["fixture"]["instances"]["first"]["env"][
            "PASSWORD"
        ] = "restored-fixture-only"
        return fragment

    def test_merge_preserves_globals_and_unselected_instances_without_mutating_inputs(
        self,
    ):
        original = deepcopy(self.config)
        fragment = self.fragment()
        archived = deepcopy(fragment)
        merged = merge_service_config(self.config, fragment, ["fixture:first"])
        self.assertEqual(merged["dumb"], self.config["dumb"])
        self.assertEqual(merged["backup_storage"], self.config["backup_storage"])
        self.assertEqual(
            merged["fixture"]["instances"]["second"],
            self.config["fixture"]["instances"]["second"],
        )
        self.assertEqual(merged["other"], self.config["other"])
        self.assertEqual(
            merged["fixture"]["instances"]["first"],
            fragment["fixture"]["instances"]["first"],
        )
        self.assertEqual(self.config, original)
        self.assertEqual(fragment, archived)

    def test_archive_cannot_smuggle_global_settings_or_unselected_instance(self):
        for key, value in (
            ("dumb", {"onboarding_completed": False}),
            ("puid", 999),
            ("other", self.config["other"]),
        ):
            fragment = self.fragment()
            fragment[key] = value
            with self.subTest(key=key), self.assertRaises(ServiceBackupScopeError):
                merge_service_config(self.config, fragment, ["fixture:first"])
        fragment = self.fragment()
        fragment["fixture"]["instances"]["second"] = deepcopy(
            self.config["fixture"]["instances"]["second"]
        )
        with self.assertRaisesRegex(ServiceBackupScopeError, "unselected"):
            merge_service_config(self.config, fragment, ["fixture:first"])

    def test_selected_service_can_be_recreated_using_current_default_template(self):
        defaults = deepcopy(self.config)
        current = deepcopy(self.config)
        current["fixture"]["instances"].pop("first")
        result = merge_service_config(
            current, self.fragment(), ["fixture:first"], defaults=defaults
        )
        self.assertIn("first", result["fixture"]["instances"])
        current.pop("fixture")
        result = merge_service_config(
            current, self.fragment(), ["fixture:first"], defaults=defaults
        )
        self.assertEqual(set(result["fixture"]["instances"]), {"first"})

    def test_unknown_service_or_setting_cannot_escape_default_contract(self):
        fragment = self.fragment()
        fragment["fixture"]["instances"]["first"]["unexpected"] = "fixture"
        with self.assertRaisesRegex(ServiceBackupScopeError, "unsupported setting"):
            merge_service_config(self.config, fragment, ["fixture:first"])
        current = deepcopy(self.config)
        current.pop("fixture")
        with self.assertRaisesRegex(ServiceBackupScopeError, "configuration template"):
            merge_service_config(current, self.fragment(), ["fixture:first"])

    def test_arbitrary_native_environment_keys_are_preserved(self):
        fragment = self.fragment()
        fragment["fixture"]["instances"]["first"]["env"][
            "NEW_NATIVE_SETTING"
        ] = "fixture"
        result = merge_service_config(self.config, fragment, ["fixture:first"])
        self.assertEqual(
            result["fixture"]["instances"]["first"]["env"]["NEW_NATIVE_SETTING"],
            "fixture",
        )

    def test_structural_scalar_types_are_checked_without_schema(self):
        fragment = self.fragment()
        fragment["fixture"]["instances"]["first"]["enabled"] = "true"
        with self.assertRaisesRegex(ServiceBackupScopeError, "invalid type"):
            merge_service_config(self.config, fragment, ["fixture:first"])

    def test_schema_failure_is_sanitized_and_cannot_be_skipped(self):
        schema = {
            "type": "object",
            "properties": {
                "fixture": {
                    "type": "object",
                    "properties": {
                        "instances": {
                            "type": "object",
                            "additionalProperties": {
                                "type": "object",
                                "properties": {
                                    "env": {
                                        "type": "object",
                                        "properties": {
                                            "PASSWORD": {"const": "another-fixture"}
                                        },
                                    }
                                },
                            },
                        }
                    },
                }
            },
        }
        with self.assertRaisesRegex(
            ServiceBackupScopeError, "current DUMB schema"
        ) as raised:
            merge_service_config(
                self.config, self.fragment(), ["fixture:first"], schema=schema
            )
        self.assertNotIn("restored-fixture-only", str(raised.exception))

    def test_service_instance_layout_changes_and_process_conflicts_are_blocked(self):
        current = deepcopy(self.config)
        current["fixture"] = {"process_name": "New singleton", "enabled": True}
        with self.assertRaisesRegex(ServiceBackupScopeError, "configuration template"):
            merge_service_config(current, self.fragment(), ["fixture:first"])
        fragment = self.fragment()
        fragment["fixture"]["instances"]["first"]["process_name"] = "Other service"
        with self.assertRaisesRegex(ServiceBackupScopeError, "process name conflicts"):
            merge_service_config(self.config, fragment, ["fixture:first"])

    def test_merge_cannot_introduce_a_nested_controller_or_auxiliary_name_collision(
        self,
    ):
        for name in ("DUMB API", "pgAgent"):
            fragment = self.fragment()
            fragment["fixture"]["instances"]["first"]["process_name"] = name
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(ServiceBackupScopeError, "shared or reserved"),
            ):
                merge_service_config(self.config, fragment, ["fixture:first"])

    def test_nonfinite_or_native_non_json_values_are_rejected(self):
        for value in (float("nan"), object()):
            fragment = self.fragment()
            fragment["fixture"]["instances"]["first"]["env"]["MALFORMED"] = value
            with (
                self.subTest(kind=type(value)),
                self.assertRaisesRegex(ServiceBackupScopeError, "unsupported values"),
            ):
                merge_service_config(self.config, fragment, ["fixture:first"])


class ServiceBackupAffectedTests(ServiceBackupFixture):
    def running(self, key, name):
        return {"key": key, "name": name, "pid": 42}

    def test_only_selected_instance_and_its_actual_waiting_consumer_are_paused(self):
        self.config["other"]["wait_for_dir"] = str(self.first)
        running = [
            self.running("fixture", "First service"),
            self.running("fixture", "Second service"),
            self.running("other", "Other service"),
        ]
        original = deepcopy(running)
        self.assertEqual(
            affected_running_rows(self.config, ["fixture:first"], running),
            [running[0], running[2]],
        )
        self.assertEqual(running, original)

    def test_transitive_wait_dependants_preserve_original_resume_order(self):
        self.config["other"]["wait_for_dir"] = str(self.first)
        self.config["downstream"] = {
            "process_name": "Downstream",
            "wait_for_dir": self.config["other"]["config_dir"],
        }
        running = [
            self.running("fixture", "First service"),
            self.running("other", "Other service"),
            self.running("fixture", "Second service"),
            self.running("downstream", "Downstream"),
        ]
        self.assertEqual(
            affected_running_rows(self.config, ["fixture:first"], running),
            [running[0], running[1], running[3]],
        )

    @isolated_dependency_test
    def test_conditional_consumers_pause_without_shared_database_provider(self):
        self.config["sonarr"] = self.config.pop("fixture")
        self.config["bazarr"] = {"process_name": "Subtitles", "enabled": True}
        self.config["postgres"] = {"process_name": "PostgreSQL", "enabled": True}
        running = [
            self.running("postgres", "PostgreSQL"),
            self.running("sonarr", "First service"),
            self.running("sonarr", "Second service"),
            self.running("bazarr", "Subtitles"),
        ]
        self.assertEqual(
            affected_running_rows(self.config, ["sonarr:first"], running),
            [running[1], running[3]],
        )

    def test_disabled_stopped_selected_service_does_not_pause_other_instances(self):
        running = [self.running("fixture", "Second service")]
        self.assertEqual(
            affected_running_rows(self.config, ["fixture:first"], running), []
        )

    def test_stopped_selected_provider_still_pauses_its_running_wait_consumer(self):
        self.config["other"]["wait_for_dir"] = str(self.first)
        running = [self.running("other", "Other service")]
        self.assertEqual(
            affected_running_rows(self.config, ["fixture:first"], running), running
        )

    @isolated_dependency_test
    def test_stopped_selected_sonarr_still_pauses_its_running_bazarr_consumer(self):
        self.config["sonarr"] = self.config.pop("fixture")
        self.config["bazarr"] = {"process_name": "Subtitles", "enabled": True}
        running = [
            self.running("sonarr", "Second service"),
            self.running("bazarr", "Subtitles"),
        ]
        self.assertEqual(
            affected_running_rows(self.config, ["sonarr:first"], running),
            [running[1]],
        )


if __name__ == "__main__":
    unittest.main()
