"""Persistence-hold authentication remains read-only and available."""

from pathlib import Path
import subprocess
import sys
import unittest


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
