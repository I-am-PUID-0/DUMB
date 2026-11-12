"""Exercise authenticated storage routes without probing a mounted destination."""

from pathlib import Path
import subprocess
import sys
import textwrap
import unittest


class BackupStorageAPITests(unittest.TestCase):
    def test_routes_auth_strict_inputs_download_and_sanitized_failures(self):
        # Fresh imports avoid the deliberate module doubles used by older tests.
        code = textwrap.dedent("""
            import asyncio
            from contextlib import contextmanager
            import io
            import json
            from types import SimpleNamespace
            from unittest.mock import Mock, patch
            from urllib.parse import urlencode
            from api.api_service import create_app
            from api.routers import backup_storage as router
            from utils.backup_storage import BackupStorageError

            with patch('api.api_service.get_logger', return_value=Mock()):
                app = create_app()

            def request(method, path, payload=None, authenticated=True, query=None):
                async def invoke():
                    messages = []
                    complete = asyncio.Event()
                    delivered = False
                    async def receive():
                        nonlocal delivered
                        if not delivered:
                            delivered = True
                            return {'type':'http.request', 'body':json.dumps(payload).encode() if payload is not None else b'', 'more_body':False}
                        await complete.wait()
                        return {'type':'http.disconnect'}
                    async def send(message):
                        messages.append(message)
                        if message['type']=='http.response.body' and not message.get('more_body'):
                            complete.set()
                    headers = [(b'content-type', b'application/json')]
                    if authenticated:
                        headers.append((b'authorization',b'Bearer fixture'))
                    scope = {'type':'http','asgi':{'version':'3.0','spec_version':'2.4'},'http_version':'1.1','method':method,'scheme':'http','path':path,'raw_path':path.encode(),'query_string':urlencode(query or {}).encode(),'root_path':'','server':('localhost',8000),'client':('127.0.0.1',1),'headers':headers}
                    await app(scope, receive, send)
                    start = next(m for m in messages if m['type']=='http.response.start')
                    raw = b''.join(m.get('body',b'') for m in messages if m['type']=='http.response.body')
                    return SimpleNamespace(status=start['status'],raw=raw,headers=dict(start['headers']),json=lambda:json.loads(raw))
                return asyncio.run(invoke())

            storage = Mock()
            storage.profiles.return_value = {'protocol':1,'destinations':[],'default_destination_id':''}
            storage.preview.return_value = {'protocol':1,'ready':True,'profile':{'id':'nas'}}
            storage.save_profile.return_value = {'protocol':1,'profile':{'id':'nas','registered':True}}
            storage.set_default.return_value = {'protocol':1,'default_destination_id':'nas'}
            storage.update_profile.return_value = {'protocol':1,'profile':{'id':'nas','name':'Edited'}}
            storage.remove_profile.return_value = {'protocol':1,'destination_id':'nas','removed':True,'archives_preserved':True,'receipts_preserved':True}
            storage.check_profile.return_value = {'protocol':1,'profile':{'id':'nas','ready':True,'live_checked':True,'validation_state':'ready','revalidation_required':False},'checked_at':123,'usage':[]}
            storage.preview_revalidation.return_value = {'protocol':1,'ready':True,'token':'a'*64,'expires_at':999,'revalidation_required':True,'changes':['mount_id'],'blockers':[]}
            storage.revalidate_profile.return_value = storage.check_profile.return_value
            storage.list_artifacts.return_value = [{'reference':'opaque'}]
            closed = []
            @contextmanager
            def artifact(reference):
                try:
                    yield io.BytesIO(b'verified cold artifact')
                finally:
                    closed.append(reference)
            storage.open_artifact.side_effect = artifact
            reference = 'dumb-backup://nas/symlink-example/' + 'a'*32
            profile = {'id':'nas','name':'Mounted backup','path':'/backup-volume','enabled':True}
            with patch.object(router,'get_backup_storage',return_value=storage), patch('utils.auth_config.AuthConfigManager.is_auth_enabled',return_value=True), patch('utils.auth.decode_token',return_value=SimpleNamespace(type='access',sub='example')), patch('utils.auth_config.AuthConfigManager.validate_token_principal',return_value=True):
                for method, suffix in [('GET',''),('POST','/preview'),('POST','/profiles'),('POST','/default'),('GET','/artifacts'),('GET','/download'),('PUT','/profiles/nas'),('DELETE','/profiles/nas'),('POST','/profiles/nas/check'),('POST','/profiles/nas/revalidate/preview'),('POST','/profiles/nas/revalidate')]:
                    assert request(method,'/process/backup-storage'+suffix,profile,False).status == 401, suffix
                storage.profiles.assert_not_called()
                assert request('GET','/process/backup-storage').json() == storage.profiles.return_value
                assert request('POST','/process/backup-storage/preview',profile).json()['ready'] is True
                assert request('POST','/process/backup-storage/profiles',profile).json()['profile']['registered'] is True
                assert storage.save_profile.call_args.args[0]['allow_shared_permissions'] is False
                assert request('POST','/process/backup-storage/profiles',{**profile,'allow_shared_permissions':True}).status == 200
                assert storage.save_profile.call_args.args[0]['allow_shared_permissions'] is True
                assert request('POST','/process/backup-storage/default',{'destination_id':'nas'}).status == 200
                assert request('PUT','/process/backup-storage/profiles/nas',profile).json()['profile']['name']=='Edited'
                assert storage.update_profile.call_args.args[0]=='nas'
                assert storage.update_profile.call_args.args[1]['id']=='nas'
                assert request('DELETE','/process/backup-storage/profiles/nas').json()['archives_preserved'] is True
                storage.remove_profile.assert_called_once_with('nas')
                assert request('POST','/process/backup-storage/profiles/nas/check').json()['profile']['live_checked'] is True
                assert request('POST','/process/backup-storage/profiles/nas/revalidate/preview').json()['revalidation_required'] is True
                assert request('POST','/process/backup-storage/profiles/nas/revalidate',{'token':'a'*64,'confirm':True}).status==200
                storage.revalidate_profile.assert_called_once_with('nas',{'token':'a'*64,'confirm':True})
                for invalid in ({'token':'short','confirm':True},{'token':'a'*64,'confirm':'true'},{'token':'a'*64,'confirm':True,'path':'/other'}):
                    assert request('POST','/process/backup-storage/profiles/nas/revalidate',invalid).status==422
                assert request('GET','/process/backup-storage/artifacts',query={'destination_id':'nas','namespace':'symlink-example'}).json()['artifacts'] == [{'reference':'opaque'}]
                storage.list_artifacts.assert_called_once_with('nas','symlink-example')
                response=request('GET','/process/backup-storage/download',query={'reference':reference})
                assert response.status == 200 and response.raw == b'verified cold artifact'
                assert closed == [reference]
                assert response.headers[b'cache-control'] == b'no-store'
                assert b'attachment;' in response.headers[b'content-disposition']
                for invalid in ({**profile,'enabled':'false'},{**profile,'id':'../escape'},{**profile,'password':'unused'},{**profile,'allow_shared_permissions':'true'},{**profile,'allow_shared_permissions':1}):
                    assert request('POST','/process/backup-storage/profiles',invalid).status == 422
                with patch.object(router,'get_project_version',return_value='2.23.0'):
                    assert request('POST','/process/backup-storage/profiles',profile).status == 426
                    assert request('GET','/process/backup-storage').status == 200
                    for method,suffix,payload in [('PUT','/profiles/nas',profile),('DELETE','/profiles/nas',None),('POST','/profiles/nas/check',None),('POST','/profiles/nas/revalidate/preview',None),('POST','/profiles/nas/revalidate',{'token':'a'*64,'confirm':True})]:
                        assert request(method,'/process/backup-storage'+suffix,payload).status==426
                storage.set_default.side_effect=BackupStorageError('The destination is missing.')
                result=request('POST','/process/backup-storage/default',{'destination_id':'nas'})
                assert result.status==409 and result.json()['detail']=='The destination is missing.'
                storage.save_profile.side_effect=OSError('private backend detail')
                result=request('POST','/process/backup-storage/profiles',profile)
                assert result.status==409 and b'private backend detail' not in result.raw
                storage.open_artifact.side_effect=BackupStorageError('private receipt detail')
                result=request('GET','/process/backup-storage/download',query={'reference':reference})
                assert result.status==409 and b'private receipt detail' not in result.raw
                capabilities=request('GET','/process/capabilities').json()
                assert capabilities['backup_storage_selection'] is True
                assert capabilities['backup_storage_protocol']==1
                assert capabilities['backup_storage_shared_permissions'] is True
                assert capabilities['backup_storage_profile_management'] is True
                assert capabilities['backup_storage_revalidation'] is True
                assert capabilities['backup_storage_management_protocol']==1
                assert capabilities['installation_backup_shared_permissions'] is True
                assert isinstance(capabilities['backup_storage_kinds'], list)
            """)
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
