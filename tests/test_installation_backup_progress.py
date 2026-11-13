"""Actual cold-census, chunk and readback progress without archive-policy changes."""

from contextlib import closing
from copy import deepcopy
import io
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

from tests import test_installation_backup as fixtures
from utils.installation_backup import InstallationBackupError
from utils.installation_backup_archive import (
    InstallationArchiveError,
    inspect_archive,
    write_archive,
)


class ArchiveProgressTests(unittest.TestCase):
    setUp = fixtures.ArchiveTests.setUp

    def payload(self):
        value = b"reserved-progress-payload:" * 135000
        self.source.joinpath("payload.bin").write_bytes(value)
        return value

    def test_real_chunks_preserve_archive_bytes_and_report_all_passes(self):
        payload = self.payload()
        baseline = io.BytesIO()
        write_archive(baseline, self.metadata, self.roots, self.scratch)
        output = io.BytesIO()
        events = []

        def observe(**event):
            events.append(event | {"stream_position": output.tell()})

        write_archive(output, self.metadata, self.roots, self.scratch, progress=observe)
        self.assertEqual(output.getvalue(), baseline.getvalue())
        phases = list(dict.fromkeys(row["stage"] for row in events))
        self.assertEqual(phases, ["inventorying", "archiving", "checking_source"])
        inventory = [row for row in events if row["stage"] == "inventorying"]
        self.assertTrue(all(row["stream_position"] == 0 for row in inventory))
        self.assertTrue(all(row["bytes_processed"] == 0 for row in inventory))
        self.assertEqual(inventory[-1]["entries"], 2)
        self.assertEqual(inventory[-1]["logical_bytes"], len(payload))
        streamed = [row for row in events if row["stage"] == "archiving"]
        self.assertEqual(streamed[-1]["bytes_processed"], len(output.getvalue()))
        self.assertEqual(streamed[-1]["entries"], 2)
        self.assertTrue(
            any(0 < row["logical_bytes"] < len(payload) for row in streamed)
        )
        source = [row for row in events if row["stage"] == "checking_source"]
        self.assertTrue(
            any(0 < row["bytes_processed"] < len(payload) for row in source)
        )
        self.assertEqual(source[-1]["bytes_processed"], len(payload))
        verified = []
        proof = inspect_archive(
            io.BytesIO(output.getvalue()),
            self.scratch,
            progress=lambda **row: verified.append(row),
        )
        self.assertTrue(proof["verified"])
        self.assertTrue(
            any(0 < row["logical_bytes"] < len(payload) for row in verified)
        )
        self.assertEqual(verified[-1]["entries"], 2)
        self.assertEqual(verified[-1]["logical_bytes"], len(payload))
        self.assertEqual(verified[-1]["bytes_processed"], len(output.getvalue()))
        for row in events + verified:
            for key in ("entries", "logical_bytes", "bytes_processed"):
                self.assertIsInstance(row[key], int)
                self.assertGreaterEqual(row[key], 0)
        self.assertEqual(list(self.scratch.glob(".installation-manifest-*")), [])

    def test_corrupted_content_is_still_refused_after_readback_progress(self):
        payload = self.payload()
        output = io.BytesIO()
        write_archive(output, self.metadata, self.roots, self.scratch)
        raw = bytearray(output.getvalue())
        index = raw.index(payload[:100]) + 1234
        raw[index] ^= 1
        events = []
        with self.assertRaisesRegex(InstallationArchiveError, "checksums or metadata"):
            inspect_archive(
                io.BytesIO(raw), self.scratch, progress=lambda **row: events.append(row)
            )
        self.assertTrue(any(row["bytes_processed"] > 0 for row in events))
        self.assertEqual(len([row for row in events if row["force"]]), 1)
        self.assertEqual(list(self.scratch.glob(".installation-manifest-*")), [])

    def test_source_identity_checks_survive_chunk_progress_callbacks(self):
        self.payload()
        changed = []

        def mutate(**row):
            if (
                row["stage"] == "checking_source"
                and row["bytes_processed"]
                and not changed
            ):
                changed.append(True)
                with self.source.joinpath("payload.bin").open("ab") as handle:
                    handle.write(b"changed-during-source-check")

        from utils.infinidysk_v2_inventory import InventoryError

        with self.assertRaisesRegex(InventoryError, "changed while being read"):
            write_archive(
                io.BytesIO(), self.metadata, self.roots, self.scratch, progress=mutate
            )
        self.assertEqual(changed, [True])
        self.assertEqual(list(self.scratch.glob(".installation-manifest-*")), [])


class ManagerBackupProgressTests(unittest.TestCase):
    join_worker = fixtures.ManagerTests.join_worker
    start_backup = fixtures.ManagerTests.start_backup

    def setUp(self):
        fixtures.ManagerTests.setUp(self)
        self.release = threading.Event()
        self.addCleanup(self.release_worker)

    def release_worker(self):
        self.release.set()
        self.join_worker()

    def request(self):
        preview = self.manager.preview({"destination_id": "fixture"})
        return {
            "preview_token": preview["preview_token"],
            "confirmation": preview["confirmation_text"],
            "acknowledgements": {
                "downtime": True,
                "secrets": True,
                "external_state": True,
            },
        }

    def test_cold_census_has_status_before_any_archive_bytes_and_no_long_lock(self):
        from utils import installation_backup_archive as archive

        request = self.request()
        entered, paused, updates = threading.Event(), [], []
        original_rows = archive.source_rows
        original_progress = self.manager._progress

        def held_rows(*args, **kwargs):
            with closing(original_rows(*args, **kwargs)) as rows:
                for row in rows:
                    yield row
                    if not paused:
                        paused.append(True)
                        entered.set()
                        self.assertTrue(self.release.wait(10))

        def capture(identifier, stage, message, **fields):
            if "progress" in fields:
                updates.append({"stage": stage, **deepcopy(fields["progress"])})
            return original_progress(identifier, stage, message, **fields)

        with (
            patch.object(archive, "source_rows", held_rows),
            patch.object(self.manager, "_progress", capture),
        ):
            accepted = self.manager.start(request)
            self.assertTrue(entered.wait(5))
            job = self.manager.status()["job"]
            self.assertEqual(job["job_id"], accepted["job"]["job_id"])
            self.assertEqual(job["status"], "running")
            self.assertEqual(job["stage"], "inventorying")
            self.assertEqual(job["progress"]["bytes_processed"], 0)
            self.assertIsInstance(job["updated_at"], (int, float))
            partials = list(
                self.root.joinpath(
                    "archives", ".dumb-backups", "fixture", "installation"
                ).glob(".*.partial")
            )
            self.assertEqual(len(partials), 1)
            self.assertEqual(partials[0].stat().st_size, 0)
            with self.assertRaisesRegex(
                InstallationBackupError, "writer is still live"
            ):
                self.manager.rollback(
                    job["job_id"], {"confirmation": job["rollback_confirmation_text"]}
                )
            self.release.set()
            self.join_worker()
        final = self.manager.status()["job"]
        self.assertEqual(final["status"], "completed", final)
        self.assertEqual(
            list(dict.fromkeys(row["stage"] for row in updates)),
            ["inventorying", "archiving", "checking_source", "verifying"],
        )
        self.assertEqual(
            final["progress"]["bytes_processed"],
            final["result"]["archive"]["size_bytes"],
        )

    def test_verification_failure_does_not_publish_even_after_real_progress(self):
        original = self.manager._backup_verifier

        def reject(*args, **kwargs):
            original(*args, **kwargs)
            raise InstallationArchiveError("Fixture readback was not accepted.")

        with patch.object(self.manager, "_backup_verifier", reject):
            job = self.start_backup()
        self.assertEqual(job["status"], "failed", job)
        self.assertGreater(job["progress"]["bytes_processed"], 0)
        self.assertIsNone(job.get("result"))
        self.assertEqual(self.storage.list_artifacts("fixture", "installation"), [])
        self.assertEqual(self.handler.events[-1], ("start", "App"))
        self.assertEqual(list(self.root.joinpath("archives").rglob("*.partial")), [])

    def test_status_updates_are_throttled_but_phase_changes_are_immediate(self):
        now, observed = [100.0], []

        def capture(_identifier, stage, _message, **fields):
            observed.append((stage, fields["progress"]))

        with (
            patch.object(self.manager, "_progress", capture),
            patch(
                "utils.installation_backup.time.monotonic", side_effect=lambda: now[0]
            ),
        ):
            report = self.manager._backup_progress("a" * 32)
            report(stage="inventorying", entries=1)
            now[0] = 100.25
            report(stage="inventorying", entries=2)
            now[0] = 101.1
            report(stage="inventorying", entries=3)
            report(stage="archiving", entries=0)
        self.assertEqual(len(observed), 3)
        self.assertEqual(observed[1][1]["entries"], 3)
        self.assertEqual(observed[2][0], "archiving")
        self.assertEqual(observed[2][1]["entries"], 0)


class PersistenceHoldMessageTests(unittest.TestCase):
    def test_actual_middleware_keeps_read_allowlist_and_string_detail(self):
        # Extract only the actual middleware into a fresh interpreter: importing
        # the complete app would initialize unrelated controller dependencies.
        script = r"""
import ast,asyncio,json,sys
from pathlib import Path
from types import ModuleType,SimpleNamespace
from fastapi.responses import JSONResponse
tree=ast.parse(Path("api/api_service.py").read_text())
node=next(node for node in ast.walk(tree) if isinstance(node,ast.AsyncFunctionDef) and node.name=="namespace_mutation_admission")
node.decorator_list=[]
module=ModuleType("utils.installation_backup")
held=[True]
module.installation_backup_blocker=lambda:"held" if held[0] else None
sys.modules["utils.installation_backup"]=module
namespace={"JSONResponse":JSONResponse,"_UNSAFE_HTTP_METHODS":frozenset({"POST","PUT","PATCH","DELETE"}),"_requires_namespace_mutation_reservation":lambda *_args:False}
exec(compile(ast.Module(body=[node],type_ignores=[]),"api/api_service.py","exec"),namespace)
reached=[]
async def downstream(request):
    reached.append(request.url.path)
    return JSONResponse({"ok":True})
async def prove():
    guard=namespace["namespace_mutation_admission"]
    expected="An installation backup, restore or recovery is controlling persistence. Use Backup and restore for status."
    for method,path in [("GET","/process/setup/runs/latest"),("HEAD","/config"),("POST","/process/setup/runs")]:
        response=await guard(SimpleNamespace(method=method,url=SimpleNamespace(path=path)),downstream)
        assert response.status_code==409
        detail=json.loads(response.body)["detail"]
        assert isinstance(detail,str) and detail==expected
        assert path not in reached
    for path in ["/process/processes","/process/installation-backups/status","/process/backup-storage","/auth/session"]:
        response=await guard(SimpleNamespace(method="GET",url=SimpleNamespace(path=path)),downstream)
        assert response.status_code==200
    for path in ["/auth/login","/auth/refresh","/auth/logout","/auth/verify"]:
        response=await guard(SimpleNamespace(method="POST",url=SimpleNamespace(path=path)),downstream)
        assert response.status_code==200
    for method,path in [
        ("POST","/auth/verify-extra"),("PUT","/auth/verify"),
        ("POST","/auth/users"),("POST","/auth/setup"),
        ("POST","/auth/oidc/callback"),("PUT","/auth/provider"),
        ("POST","/process/start"),("PATCH","/config"),("DELETE","/auth/users/fixture")]:
        response=await guard(SimpleNamespace(method=method,url=SimpleNamespace(path=path)),downstream)
        assert response.status_code==409
    held[0]=False
    response=await guard(SimpleNamespace(method="GET",url=SimpleNamespace(path="/process/setup/runs/latest")),downstream)
    assert response.status_code==200
asyncio.run(prove())
"""
        result = subprocess.run(
            [sys.executable, "-B", "-c", script],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr[-4000:])

    def test_actual_token_verification_is_stateless_during_persistence_hold(self):
        script = r"""
import ast,os,sys
from pathlib import Path
from types import SimpleNamespace
tree=ast.parse(Path("api/routers/auth.py").read_text())
node=next(node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name=="verify_token")
node.decorator_list=[]
enabled,allowed,payload=[True],[True],[SimpleNamespace(type="access",sub="reserved-fixture")]
namespace={
    "AUTH_CONFIG":SimpleNamespace(
        is_auth_enabled=lambda:enabled[0],
        validate_token_principal=lambda value:allowed[0]),
    "decode_token":lambda token:payload[0],
    "VerifyResponse":lambda **values:SimpleNamespace(**values),
}
exec(compile(ast.Module(body=[node],type_ignores=[]),"api/routers/auth.py","exec"),namespace)
mutations=[]
def audit(event,args):
    forbidden=event.startswith(("socket.","subprocess.")) or event in {
        "os.rename","os.remove","os.mkdir","os.rmdir","os.chmod","os.chown","os.truncate"}
    if event=="open":
        mode,flags=args[1],args[2]
        forbidden=bool((isinstance(mode,str) and any(char in mode for char in "wax+")) or
                       (isinstance(flags,int) and flags & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC)))
    if forbidden:
        mutations.append(event)
        raise AssertionError("Verification attempted a persistence or network mutation")
sys.addaudithook(audit)
verify=namespace["verify_token"]
assert verify("reserved-fixture").valid
allowed[0]=False
assert not verify("reserved-fixture").valid
allowed[0]=True
payload[0]=SimpleNamespace(type="refresh",sub="reserved-fixture")
assert not verify("reserved-fixture").valid
payload[0]=None
assert not verify("reserved-fixture").valid
enabled[0]=False
assert not verify("reserved-fixture").valid
assert not mutations
"""
        result = subprocess.run(
            [sys.executable, "-B", "-c", script],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr[-4000:])


if __name__ == "__main__":
    unittest.main()
