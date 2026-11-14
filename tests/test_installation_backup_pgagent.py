"""Account for DUMB's pgAgent writer without admitting unknown auxiliary processes."""

from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from utils.installation_backup import InstallationBackupError, InstallationBackupManager
from utils.installation_backup_lifecycle import BackupLifecycleError, order_resume_rows
from utils.postgres import start_pgagent


class ManagedProcess:
    def __init__(self, pid):
        self.pid, self.stopped = pid, False

    def poll(self):
        return 0 if self.stopped else None


class ManagedHandler:
    def __init__(self, prefix="", spawn_agent=False):
        self.prefix, self.spawn_agent = prefix, spawn_agent
        self.process_names, self.external_processes, self.events = {}, {}, []
        self.previous = []
        for index, name in enumerate(("pgAgent", "pgAdmin", "PostgreSQL"), 200001):
            self.process_names[self._prefixed_name(name)] = ManagedProcess(index)
        self.next_pid = 300001

    def _prefixed_name(self, name):
        return self.prefix + name

    def is_alive(self, pid):
        return any(
            process.pid == pid and process.poll() is None
            for process in self.process_names.values()
        )

    def stop_process(self, name):
        self.events.append(("stop", name))
        self.previous.append(self.process_names.pop(self._prefixed_name(name)))
        self.previous[-1].stopped = True
        return True, None

    def start_process(self, name, config_dir=None, command=None):
        # A synthetic pgAgent name has no standalone configuration. Its native
        # startup helper must supply the working directory and full command.
        if name == "pgAgent" and (config_dir != "/usr/bin/" or not command):
            return False, "Synthetic agent requires its prepared launch command."
        self.events.append(("start", name))
        self.process_names[self._prefixed_name(name)] = ManagedProcess(self.next_pid)
        self.next_pid += 1
        if name == "pgAdmin" and self.spawn_agent:
            return start_pgagent(self, "example_user", "127.0.0.1", 5432)
        return True, None


class ManagedPgAgentTests(unittest.TestCase):
    def setUp(self):
        # The native pgAdmin/pgAgent relationship must remain valid even if
        # optional family-map edges are unavailable or omit disabled services.
        dependencies = patch(
            "utils.dependency_map.build_conditional_dependency_map", return_value={}
        )
        dependencies.start()
        self.addCleanup(dependencies.stop)
        self.config = {
            "postgres": {
                "enabled": True,
                "process_name": "PostgreSQL",
                "host": "127.0.0.1",
                "port": 5432,
                "user": "example_user",
            },
            "pgadmin": {"enabled": True, "process_name": "pgAdmin"},
        }

    def manager(self, *, prefix="", spawn_agent=False):
        handler = ManagedHandler(prefix, spawn_agent)
        manager = InstallationBackupManager(
            SimpleNamespace(
                file_path="/reserved-example/dumb_config.json",
                config=deepcopy(self.config),
            ),
            handler,
            storage=Mock(),
        )
        job = {"job_type": "installation_backup", "status": "running", "errors": []}
        manager._job = lambda identifier: deepcopy(job)
        manager._check_backup_cancel = lambda identifier: None
        manager._pid_alive = handler.is_alive

        def progress(identifier, stage, message, **fields):
            job.update(stage=stage, message=message, **fields)

        manager._progress = progress
        return manager, handler, job

    def test_registered_agent_is_cold_before_database_and_resumes_once(self):
        for prefix in ("", "scope:"):
            for spawn_agent in (False, True):
                with self.subTest(prefix=prefix, pgadmin_spawns_agent=spawn_agent):
                    manager, handler, job = self.manager(
                        prefix=prefix, spawn_agent=spawn_agent
                    )
                    before = manager._running()
                    self.assertEqual(
                        [row["name"] for row in before],
                        ["PostgreSQL", "pgAdmin", "pgAgent"],
                    )
                    agent = before[-1]
                    self.assertEqual(agent["key"], "pgagent")
                    self.assertEqual(
                        agent["pid"],
                        handler.process_names[prefix + "pgAgent"].pid,
                    )

                    def cold(config):
                        self.assertEqual(handler.process_names, {})
                        self.assertEqual(
                            handler.events,
                            [
                                ("stop", "pgAgent"),
                                ("stop", "pgAdmin"),
                                ("stop", "PostgreSQL"),
                            ],
                        )

                    with patch(
                        "utils.installation_backup.verify_cold_postgres",
                        side_effect=cold,
                    ) as checked:
                        manager._quiesce("fixture")
                    checked.assert_called_once()
                    self.assertEqual(job["running_before"], before)
                    self.assertTrue(job["services_stopped"])
                    manager._resume("fixture")
                    self.assertEqual(
                        handler.events[3:],
                        [
                            ("start", "PostgreSQL"),
                            ("start", "pgAdmin"),
                            ("start", "pgAgent"),
                        ],
                    )
                    self.assertEqual(len(handler.process_names), 3)

    def test_only_recorded_agent_is_restarted_when_owner_was_not_running(self):
        manager, handler, job = self.manager()
        handler.process_names = {
            "pgAgent": handler.process_names["pgAgent"],
        }
        before = manager._running()
        self.assertEqual(
            [row["name"] for row in before],
            ["pgAgent"],
        )
        handler.stop_process("pgAgent")
        job["running_before"] = before
        manager._resume("fixture")
        self.assertEqual(handler.events, [("stop", "pgAgent"), ("start", "pgAgent")])
        self.assertEqual(list(handler.process_names), ["pgAgent"])

    def test_unknown_live_writer_remains_a_hard_refusal(self):
        manager, handler, _ = self.manager()
        handler.process_names["Unclassified"] = ManagedProcess(400001)
        with self.assertRaisesRegex(InstallationBackupError, "unclassified"):
            manager._running()
        self.assertEqual(handler.events, [])

    def test_external_agent_name_claim_is_not_a_managed_subprocess(self):
        manager, handler, _ = self.manager()
        handler.process_names.pop("pgAgent")
        handler.external_processes["pgAgent"] = 400001
        manager._pid_alive = lambda pid: True
        with self.assertRaisesRegex(InstallationBackupError, "subprocess registration"):
            manager._running()
        self.assertEqual(handler.events, [])

    def test_agent_requires_configured_owners_and_positive_registered_pid(self):
        for missing in ("pgadmin", "postgres"):
            with self.subTest(missing=missing):
                manager, handler, _ = self.manager()
                manager.config_manager.config.pop(missing)
                with self.assertRaises(InstallationBackupError):
                    manager._running()
                self.assertEqual(handler.events, [])
        for pid in (None, 0, True):
            with self.subTest(pid=pid):
                manager, handler, _ = self.manager()
                handler.process_names["pgAgent"].pid = pid
                with self.assertRaisesRegex(
                    InstallationBackupError, "subprocess registration"
                ):
                    manager._running()

    def test_configured_name_collision_does_not_adopt_an_ambiguous_agent(self):
        manager, handler, _ = self.manager()
        manager.config_manager.config["other"] = {"process_name": "pgAgent"}
        with self.assertRaisesRegex(InstallationBackupError, "one matching"):
            manager._running()
        self.assertEqual(handler.events, [])

    def test_native_pgadmin_dependency_survives_without_an_agent_or_family_map(self):
        rows = [
            {"key": "pgadmin", "name": "pgAdmin", "pid": 124},
            {"key": "postgres", "name": "PostgreSQL", "pid": 125},
        ]
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                configuration = deepcopy(self.config)
                for section in configuration.values():
                    section["enabled"] = enabled
                result = order_resume_rows(rows, configuration, {})
                self.assertEqual(
                    [row["name"] for row in result], ["PostgreSQL", "pgAdmin"]
                )
                self.assertEqual(sorted(row["pid"] for row in result), [124, 125])

    def test_legacy_ordering_puts_agent_after_both_retained_dependencies(self):
        rows = [
            {"key": "pgagent", "name": "pgAgent", "pid": 123},
            {"key": "pgadmin", "name": "pgAdmin", "pid": 124},
            {"key": "postgres", "name": "PostgreSQL", "pid": 125},
        ]
        result = order_resume_rows(rows, self.config, {})
        self.assertEqual(
            [row["name"] for row in result],
            ["PostgreSQL", "pgAdmin", "pgAgent"],
        )
        changed = deepcopy(self.config)
        changed.pop("pgadmin")
        with self.assertRaises(BackupLifecycleError):
            order_resume_rows(rows, changed, {})


if __name__ == "__main__":
    unittest.main()
