import copy
import unittest

from utils.infinidysk_v2_stages import (
    checkpoint,
    public_stage_progress,
    stage_plan,
    storage_work,
)


class MigrationStagesTests(unittest.TestCase):
    def job(self, mode="prepare", provider="sqlite", fresh=False):
        plan = {
            "mode": "fresh" if fresh else "migrate",
            "source": {"provider": provider},
        }
        return {"mode": mode, "plan": plan, "stage_progress": stage_plan(plan, mode)}

    def states(self, value):
        return {item["id"]: item["state"] for item in value["steps"]}

    def test_applicable_steps_include_postgres_and_prepare_review(self):
        job = self.job(provider="postgres")
        self.assertEqual(
            list(self.states(job["stage_progress"])),
            [
                "check_plan",
                "pause_services",
                "source_database",
                "save_recovery",
                "prepare_input",
                "native_import",
                "validate_import",
                "review",
                "activate",
                "resume_services",
            ],
        )
        fresh = self.job(mode="cutover", fresh=True)
        self.assertNotIn("source_database", self.states(fresh["stage_progress"]))
        self.assertNotIn("review", self.states(fresh["stage_progress"]))

    def test_next_stage_never_infers_previous_completion(self):
        job = self.job()
        checkpoint(job, "snapshotting", now=10)
        checkpoint(job, "importing", now=20)
        result = public_stage_progress(job, "running")
        self.assertEqual(self.states(result)["save_recovery"], "running")
        self.assertNotIn("completed", self.states(result).values())
        self.assertEqual(result["current_step_id"], "native_import")

    def test_completed_helpers_survive_restart_without_becoming_live(self):
        job = self.job()
        checkpoint(job, "revalidating", now=10)
        checkpoint(job, "quiescing", completed=["check_plan"], now=20)
        before = copy.deepcopy(job)
        result = public_stage_progress(job, "interrupted")
        self.assertEqual(self.states(result)["check_plan"], "completed")
        self.assertEqual(self.states(result)["pause_services"], "interrupted")
        self.assertEqual(result["steps"][0]["completed_at"], 20)
        self.assertEqual(job, before)

    def test_prepare_waits_for_user_and_activation_records_consent_only(self):
        job = self.job()
        checkpoint(
            job, "accepted", completed=["native_import", "validate_import"], now=20
        )
        ready = public_stage_progress(job, "prepared")
        self.assertEqual(self.states(ready)["review"], "waiting")
        self.assertEqual(self.states(ready)["activate"], "pending")
        checkpoint(job, "activating", completed=["review"], now=30)
        active = public_stage_progress(job, "running")
        self.assertEqual(self.states(active)["review"], "completed")
        self.assertEqual(self.states(active)["activate"], "running")
        self.assertEqual(self.states(active)["resume_services"], "pending")

    def test_failed_import_and_verified_restoration_are_distinct(self):
        job = self.job()
        checkpoint(job, "validating", completed=["native_import"], now=20)
        checkpoint(job, "rolling_back", recovery=True, now=30)
        checkpoint(job, "restored", completed=["restore_v1"], now=40)
        result = self.states(public_stage_progress(job, "failed_restored"))
        self.assertEqual(result["validate_import"], "failed")
        self.assertEqual(result["native_import"], "completed")
        self.assertEqual(result["restore_v1"], "completed")
        self.assertEqual(result["activate"], "not_run")

    def test_cancel_before_execution_does_not_claim_restoration(self):
        job = self.job()
        checkpoint(job, "revalidating", now=10)
        result = self.states(public_stage_progress(job, "cancelled"))
        self.assertEqual(result["check_plan"], "interrupted")
        self.assertEqual(result["pause_services"], "not_run")
        self.assertNotIn("restore_v1", result)

    def test_rehearsal_has_restore_and_cleanup_but_no_activation(self):
        job = self.job(mode="rehearsal")
        result = self.states(job["stage_progress"])
        self.assertIn("restore_v1", result)
        self.assertIn("cleanup_test", result)
        self.assertNotIn("activate", result)
        self.assertNotIn("review", result)

    def test_legacy_success_or_failure_does_not_invent_completed_history(self):
        job = self.job()
        job.pop("stage_progress")
        job["stage"] = "failed"
        for state in ("failed_restored", "completed", "rehearsed", "interrupted"):
            with self.subTest(state=state):
                result = public_stage_progress(job, state)
                self.assertEqual(result["tracking"], "legacy")
                self.assertEqual(set(self.states(result).values()), {"pending"})
        checkpoint(job, "importing")
        self.assertNotIn("stage_progress", job)

    def test_legacy_current_phase_and_recovery_are_visible_without_predecessors(self):
        job = {"mode": "prepare", "plan": {"mode": "migrate"}, "stage": "importing"}
        result = self.states(public_stage_progress(job, "running"))
        self.assertEqual(result["native_import"], "running")
        self.assertEqual(result["save_recovery"], "pending")
        job["stage"] = "rolling_back"
        result = self.states(public_stage_progress(job, "rolling_back"))
        self.assertEqual(result["restore_v1"], "running")

    def test_post_exposure_consumer_hold_never_completes_resumption(self):
        job = self.job()
        checkpoint(
            job,
            "resuming_consumers",
            completed=["activate"],
            current="resume_services",
            now=20,
        )
        result = self.states(public_stage_progress(job, "recovery_required"))
        self.assertEqual(result["activate"], "completed")
        self.assertEqual(result["resume_services"], "interrupted")
        self.assertNotIn("restore_v1", result)

    def test_invalid_private_rows_are_not_exposed(self):
        job = self.job()
        job["stage_progress"]["steps"] = [
            {"id": "secret-value", "state": "completed"},
            {
                "id": "native_import",
                "state": "completed",
                "started_at": True,
                "completed_at": -1,
                "password": "hidden",
            },
        ]
        result = public_stage_progress(job, "running")
        self.assertNotIn("secret-value", self.states(result))
        native = next(row for row in result["steps"] if row["id"] == "native_import")
        self.assertEqual(native, {"id": "native_import", "state": "completed"})

    def test_malformed_tracking_cannot_abort_a_migration_checkpoint(self):
        for value in (
            [],
            {"protocol": 1, "steps": None},
            {
                "protocol": 1,
                "steps": [None, [], {"id": [], "state": "running"}],
                "current_step_id": [],
            },
        ):
            with self.subTest(value=value):
                job = self.job()
                job["stage_progress"] = value
                checkpoint(job, "quiescing", completed=["check_plan"])
                public_stage_progress(job, "running")

    def test_cancelled_recovery_interrupts_the_original_step(self):
        job = self.job()
        job["cancel_requested"] = True
        checkpoint(job, "importing", now=10)
        checkpoint(job, "rolling_back", recovery=True, now=20)
        states = self.states(public_stage_progress(job, "rolling_back"))
        self.assertEqual(states["native_import"], "interrupted")
        self.assertEqual(states["restore_v1"], "running")

    def test_explicit_finish_with_stopped_apps_does_not_claim_resumption(self):
        job = self.job()
        checkpoint(
            job,
            "resuming_consumers",
            completed=["activate"],
            current="resume_services",
            now=10,
        )
        checkpoint(job, "consumers_left_stopped", not_run=["resume_services"], now=20)
        result = public_stage_progress(job, "completed")
        self.assertEqual(self.states(result)["resume_services"], "not_run")
        row = next(row for row in result["steps"] if row["id"] == "resume_services")
        self.assertNotIn("completed_at", row)

    def test_repeated_completions_keep_the_first_verified_timestamp(self):
        job = self.job()
        checkpoint(job, "accepted", completed=["validate_import"], now=10)
        checkpoint(job, "accepted", completed=["validate_import"], now=20)
        row = next(
            row
            for row in job["stage_progress"]["steps"]
            if row["id"] == "validate_import"
        )
        self.assertEqual(row["completed_at"], 10)


class MigrationStorageWorkTests(unittest.TestCase):
    def plan(self, **options):
        return {
            "mode": "migrate",
            "options": options,
            "capacity": {
                "complete": True,
                "source": {
                    "logical_bytes": 1200,
                    "roots": [
                        {"role": "data_dir", "logical_bytes": 1000},
                        {"role": "runtime_dir", "logical_bytes": 200},
                    ],
                },
                "legacy_blobs": {"logical_bytes": 800},
            },
            "backup_storage": {"mode": "cold_archive", "storage_type": "network"},
        }

    def paths(self, result):
        return {row["id"]: row for row in result["paths"]}

    def test_census_quantifies_extra_io_without_an_elapsed_ranking(self):
        result = storage_work(self.plan(reclaim_source_space=True))
        self.assertEqual(result["ranking"], "not_estimated")
        self.assertEqual(result["archive_bytes"], 1200)
        self.assertTrue(result["archive_bytes_is_minimum"])
        paths = self.paths(result)
        self.assertEqual(paths["keep_local"]["preparation"][0]["read_bytes"], 1000)
        self.assertEqual(paths["keep_local"]["preparation"][0]["write_bytes"], 1000)
        reuse = paths["reuse_local"]["preparation"][0]
        self.assertEqual(reuse["passes"], 2)
        self.assertEqual(reuse["read_bytes"], 2400)
        self.assertTrue(reuse["minimum"])
        self.assertEqual(paths["external_blobs"]["preparation"][2]["read_bytes"], 2400)
        self.assertEqual(result["archive_storage_type"], "network")

    def test_actual_archive_receipt_replaces_only_its_lower_bound(self):
        result = storage_work(self.plan(), {"cold_archive": {"size_bytes": 1800}})
        self.assertEqual(result["archive_bytes"], 1800)
        self.assertFalse(result["archive_bytes_is_minimum"])
        reuse = self.paths(result)["reuse_local"]
        self.assertEqual(reuse["preparation"][0]["read_bytes"], 3600)
        self.assertEqual(reuse["activation"][0]["read_bytes"], 1800)
        self.assertEqual(sum(leg["read_bytes"] for leg in reuse["restoration"]), 6400)
        self.assertEqual(sum(leg["write_bytes"] for leg in reuse["restoration"]), 1000)

    def test_unknown_and_zero_sizes_do_not_trigger_scans_or_guess_network(self):
        result = storage_work(
            {"mode": "migrate", "backup_storage": {"mode": "cold_archive"}}
        )
        self.assertIsNone(result["source_data_bytes"])
        self.assertIsNone(result["archive_bytes"])
        self.assertEqual(result["archive_storage_type"], "unknown")
        self.assertIsNone(
            self.paths(result)["external_blobs"]["preparation"][1]["read_bytes"]
        )
        plan = self.plan()
        plan["capacity"]["legacy_blobs"]["logical_bytes"] = 0
        self.assertEqual(storage_work(plan)["legacy_blob_bytes"], 0)

    def test_fresh_has_no_legacy_storage_alternatives(self):
        self.assertEqual(storage_work({"mode": "fresh"})["paths"], [])


if __name__ == "__main__":
    unittest.main()
