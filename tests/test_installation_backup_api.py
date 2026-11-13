"""Real FastAPI routing in a fresh interpreter avoids legacy discovery stubs."""

import os
from pathlib import Path
import subprocess
import sys
import unittest


class InstallationBackupAPITests(unittest.TestCase):
    def test_authenticated_routes_private_validation_and_recovery_dependencies(self):
        script = r"""
from fastapi import FastAPI, HTTPException
import asyncio
import json
from types import SimpleNamespace
from api.routers import installation_backup as routes
from utils.dependencies import get_optional_current_user, get_process_handler

class Controller:
    calls = []
    def status(self):
        self.calls.append("status")
        return {"protocol":1,"job":None,"preparation":None,"recovery_required":False,"schedule":{},"scope":{}}
    def preview(self, value):
        return {"protocol":1,"ready":True,"capacity":{"status":"sufficient"},"destination":value["destination_id"],"compression":value["compression"]}
    def preview_status(self, identifier=None):
        if identifier == "b"*32:
            from utils.installation_backup import InstallationBackupError
            raise InstallationBackupError("This preview is unavailable or expired.")
        return {"protocol":1,"controller_session":"reserved-session","preview":{"preview_id":identifier or "a"*32,"status":"running","result":None}}
    def cancel_preview(self, identifier):
        return {"protocol":1,"controller_session":"reserved-session","preview":{"preview_id":identifier,"status":"cancelling","cancel_requested":True,"result":None}}
    def start(self, value):
        return {"protocol":1,"job":{"job_id":"a"*32,"job_type":"installation_backup","status":"queued"}}
    def restore_preview(self, value):
        self.restore_options = value
        return {"protocol":1,"ready":True,"backup":{"verified":True,"archive_path":value.get("archive_path")}}
    def restore_start(self, value):
        return {"protocol":1,"job":{"job_id":"b"*32,"job_type":"installation_restore","status":"queued"}}
    def get_job(self, identifier):
        return {"protocol":1,"job":{"job_id":identifier,"job_type":"installation_backup","status":"running"}}
    def update_schedule(self, value):
        return {"protocol":1,"schedule":value}
    def rollback(self, identifier, value):
        return {"protocol":1,"job":{"job_id":identifier,"status":"rolled_back"}}
controller=Controller()
created=[]
def factory(config, handler):
    created.append(True)
    return controller
routes.get_installation_backup_manager=factory
app=FastAPI()
app.include_router(routes.installation_backup_router,prefix="/process")
app.dependency_overrides[get_process_handler]=lambda: object()
def denied():
    raise HTTPException(status_code=401,detail="Authentication required")
app.dependency_overrides[get_optional_current_user]=denied
async def request(method,path,json_payload=None,content=None):
    path,_,query=path.partition("?")
    messages=[]
    complete=asyncio.Event()
    delivered=False
    async def receive():
        nonlocal delivered
        if not delivered:
            delivered=True
            return {"type":"http.request","body":content if content is not None else json.dumps(json_payload).encode() if json_payload is not None else b"","more_body":False}
        await complete.wait()
        return {"type":"http.disconnect"}
    async def send(message):
        messages.append(message)
        if message["type"]=="http.response.body" and not message.get("more_body"):
            complete.set()
    scope={"type":"http","asgi":{"version":"3.0","spec_version":"2.4"},"http_version":"1.1",
           "method":method,"scheme":"http","path":path,"raw_path":path.encode(),"query_string":query.encode(),
           "root_path":"","server":("reserved.example",8000),"client":("127.0.0.1",1),
           "headers":[(b"content-type",b"application/json")]}
    await app(scope,receive,send)
    status=next(x["status"] for x in messages if x["type"]=="http.response.start")
    raw=b"".join(x.get("body",b"") for x in messages if x["type"]=="http.response.body")
    start=next(x for x in messages if x["type"]=="http.response.start")
    headers={key.decode():value.decode() for key,value in start.get("headers",[])}
    return SimpleNamespace(status_code=status,text=raw.decode(),headers=headers,json=lambda:json.loads(raw))
class Client:
    async def __aenter__(self): return self
    async def __aexit__(self,*_args): pass
    async def get(self,path,**_kwargs): return await request("GET",path)
    async def post(self,path,json=None,content=None,headers=None): return await request("POST",path,json,content)
    async def put(self,path,json=None): return await request("PUT",path,json)
async def prove():
    async with Client() as client:
        response=await client.get("/process/installation-backups/status")
        assert response.status_code==401, response.text
        assert not created, "unauthorized request initialized controller state"
        assert (await client.get("/process/installation-backups/jobs/"+"a"*32)).status_code==401
        assert (await client.get("/process/installation-backups/preview/status")).status_code==401
        assert (await client.post("/process/installation-backups/preview/"+"a"*32+"/cancel")).status_code==401
        assert not created, "unauthorized polling or cancellation initialized controller state"
        app.dependency_overrides[get_optional_current_user]=lambda: "reserved-fixture-admin"
        status=await client.get("/process/installation-backups/status")
        assert status.json()["protocol"]==1 and status.json()["preparation"] is None
        assert status.headers["cache-control"]=="no-store"
        job=await client.get("/process/installation-backups/jobs/"+"a"*32)
        assert job.status_code==200 and job.json()["job"]["status"]=="running"
        assert job.headers["cache-control"]=="no-store"
        preview=await client.post("/process/installation-backups/preview",json={"destination_id":"@default"})
        assert preview.status_code==200 and preview.json()["capacity"]["status"]=="sufficient"
        assert preview.headers["cache-control"]=="no-store"
        assert preview.json()["compression"]=="none"
        compressed=await client.post("/process/installation-backups/preview",json={"destination_id":"fixture","compression":"zstd"})
        assert compressed.status_code==200 and compressed.json()["compression"]=="zstd"
        unsupported=await client.post("/process/installation-backups/preview",json={"destination_id":"fixture","compression":"gzip-private-invalid"})
        assert unsupported.status_code==422 and "gzip-private-invalid" not in unsupported.text
        polled=await client.get("/process/installation-backups/preview/status?preview_id="+"a"*32)
        assert polled.status_code==200 and polled.json()["preview"]["preview_id"]=="a"*32
        assert polled.headers["cache-control"]=="no-store"
        unknown=await client.get("/process/installation-backups/preview/status?preview_id="+"b"*32)
        assert unknown.status_code==409
        invalid=await client.get("/process/installation-backups/preview/status?preview_id=private-input-must-not-echo")
        assert invalid.status_code==422 and "private-input-must-not-echo" not in invalid.text
        cancelled=await client.post("/process/installation-backups/preview/"+"a"*32+"/cancel")
        assert cancelled.status_code==200 and cancelled.json()["preview"]["status"]=="cancelling"
        assert cancelled.headers["cache-control"]=="no-store"
        invalid=await client.post("/process/installation-backups/preview/private-input-must-not-echo/cancel")
        assert invalid.status_code==409 and "private-input-must-not-echo" not in invalid.text
        malformed=await client.post("/process/installation-backups/restore/start",json={"preview_token":"fixture","confirmation":"fixture","acknowledgements":{"downtime":"private-input-must-not-echo"}})
        assert malformed.status_code==422 and "private-input-must-not-echo" not in malformed.text
        huge=await client.post("/process/installation-backups/restore/preview",content=b"x"*65537,headers={"content-type":"application/json"})
        assert huge.status_code==413
        restore=await client.post("/process/installation-backups/restore/preview",json={"archive_path":"/reserved-example/installation.tar"})
        assert restore.status_code==200 and restore.json()["backup"]["verified"] is True
        assert controller.restore_options["allow_shared_permissions"] is False
        restore=await client.post("/process/installation-backups/restore/preview",json={"archive_path":"/reserved-example/installation.tar","allow_shared_permissions":True})
        assert restore.status_code==200 and controller.restore_options["allow_shared_permissions"] is True
        for invalid_consent in ["private-consent-must-not-echo",1,None,{}]:
            malformed=await client.post("/process/installation-backups/restore/preview",json={"archive_path":"/reserved-example/installation.tar","allow_shared_permissions":invalid_consent})
            assert malformed.status_code==422 and "private-consent-must-not-echo" not in malformed.text
        schedule=await client.put("/process/installation-backups/schedule",json={"enabled":False,"destination_id":"","interval_hours":24,"keep_last":7})
        assert schedule.status_code==200 and schedule.json()["schedule"]["enabled"] is False
        assert "compression" not in schedule.json()["schedule"]
        explicit=await client.put("/process/installation-backups/schedule",json={"enabled":False,"compression":"none"})
        assert explicit.status_code==200 and explicit.json()["schedule"]["compression"]=="none"
        explicit=await client.put("/process/installation-backups/schedule",json={"enabled":False,"compression":"zstd"})
        assert explicit.status_code==200 and explicit.json()["schedule"]["compression"]=="zstd"
        invalid=await client.put("/process/installation-backups/schedule",json={"enabled":True,"destination_id":"fixture","interval_hours":1.5,"keep_last":7})
        assert invalid.status_code==422
        rollback=await client.post("/process/installation-backups/jobs/"+"a"*32+"/rollback",json={"confirmation":"ROLL BACK "+"a"*32})
        assert rollback.status_code==200
        from tests.test_installation_backup import ManagerTests
        from pathlib import Path
        import io, tarfile
        fixture=ManagerTests("test_real_backup_restore_roundtrip_and_exact_rollback")
        fixture.setUp()
        original_factory=routes.get_installation_backup_manager
        try:
            routes.get_installation_backup_manager=lambda _config,_handler:fixture.manager
            invalid=fixture.root/"invalid.tar"
            invalid.write_bytes(b"private malformed archive bytes")
            invalid.chmod(0o600)
            response=await client.post("/process/installation-backups/restore/preview",json={"archive_path":str(invalid)})
            assert response.status_code==200 and response.json()["ready"] is False
            assert "private malformed archive bytes" not in response.text
            with tarfile.open(invalid,"w") as archive:
                info=tarfile.TarInfo("ordinary.txt")
                info.size=1
                archive.addfile(info,io.BytesIO(b"x"))
            response=await client.post("/process/installation-backups/restore/preview",json={"archive_path":str(invalid)})
            assert response.status_code==200 and response.json()["ready"] is False
            assert fixture.handler.events==[]
            from utils import installation_backup_codec as codec
            from unittest.mock import patch
            fixture.manager.update_schedule({"enabled":False,"compression":"zstd"})
            assert fixture.config.config["installation_backup"]["compression"]=="zstd"
            with patch.object(codec,"zstd",None):
                assert not codec.zstd_available()
                response=await client.put("/process/installation-backups/schedule",json={"enabled":False,"compression":"none"})
                assert response.status_code==200, response.text
                assert response.json()["schedule"]["compression"]=="none"
                assert fixture.config.config["installation_backup"]["compression"]=="none"
            assert fixture.handler.events==[]
        finally:
            routes.get_installation_backup_manager=original_factory
            fixture.doCleanups()
asyncio.run(prove())
"""
        result = subprocess.run(
            [sys.executable, "-B", "-c", script],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            timeout=60,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr[-5000:])
