"""Exercise the real router, auth dependency and admission middleware together."""

from pathlib import Path
import subprocess
import sys
import textwrap
import unittest


class NativeMigrationAPITests(unittest.TestCase):
    def test_version_authentication_and_bidirectional_mutation_admission(self):
        # Existing router tests intentionally install module doubles. A fresh
        # interpreter exercises actual FastAPI dependencies regardless of order.
        code = textwrap.dedent("""
            from types import SimpleNamespace
            from unittest.mock import Mock, patch
            import asyncio
            import json
            from api.api_service import create_app
            from api.routers import infinidysk_v2 as router
            from utils import infinidysk_migration_admission as admission
            from utils.infinidysk_v2_migration import InfiniDyskV2MigrationError
            from utils.dependencies import get_process_handler, get_updater

            with patch('api.api_service.get_logger', return_value=Mock()):
                app = create_app()
            app.dependency_overrides[get_process_handler] = lambda: None
            app.dependency_overrides[get_updater] = lambda: None
            class Client:
                # Use ASGI directly so this gate needs only locked runtime
                # dependencies, including on the supported Python CI matrix.
                def __init__(self):
                    self.headers = {}
                def request(self, method, path, payload=None):
                    async def invoke():
                        messages = []
                        complete = asyncio.Event()
                        body = json.dumps(payload).encode() if payload is not None else b''
                        delivered = False
                        async def receive():
                            nonlocal delivered
                            if not delivered:
                                delivered = True
                                return {'type': 'http.request', 'body': body, 'more_body': False}
                            await complete.wait()
                            return {'type': 'http.disconnect'}
                        async def send(message):
                            messages.append(message)
                            if message['type'] == 'http.response.body' and not message.get('more_body'):
                                complete.set()
                        headers = {**self.headers, 'Content-Type': 'application/json'}
                        scope = {'type': 'http', 'asgi': {'version': '3.0', 'spec_version': '2.4'}, 'http_version': '1.1', 'method': method, 'scheme': 'http', 'path': path, 'raw_path': path.encode(), 'query_string': b'', 'root_path': '', 'server': ('localhost', 8000), 'client': ('127.0.0.1', 1), 'headers': [(key.lower().encode(), value.encode()) for key, value in headers.items()]}
                        await app(scope, receive, send)
                        start = next(message for message in messages if message['type'] == 'http.response.start')
                        raw = b''.join(message.get('body', b'') for message in messages if message['type'] == 'http.response.body')
                        return SimpleNamespace(status_code=start['status'], text=raw.decode(), headers={key.decode(): value.decode() for key, value in start['headers']}, json=lambda: json.loads(raw))
                    return asyncio.run(invoke())
                def get(self, path):
                    return self.request('GET', path)
                def post(self, path, json):
                    return self.request('POST', path, json)
                def patch(self, path, json):
                    return self.request('PATCH', path, json)
            manager = Mock()
            manager.status.return_value = {'protocol': 1, 'job': None}
            manager.get_report.return_value = 'Native fixture report\\n'
            capacity = {
                'status': 'insufficient', 'complete': True, 'estimated': True,
                'reserved': False, 'observed_at': 1,
                'source': {'entries': 10, 'logical_bytes': 1024, 'root_count': 1},
                'filesystems': [{'id': 'filesystem_1', 'roles': ['retained_backup', 'candidate'],
                    'base_required_bytes': 100, 'safety_margin_bytes': 64,
                    'required_bytes': 164, 'free_bytes': 100,
                    'shortfall_bytes': 64, 'sufficient': False}],
            }
            manager.preview.return_value = {'protocol': 1, 'ready': False, 'capacity': capacity}
            manager.start.return_value = {'job': {'job_id': 'a' * 32}}
            manager.cleanup_preview.return_value = {'protocol': 1, 'ready': False, 'blockers': ['Source ownership review is required.']}
            manager.cleanup.return_value = {'job': {'status': 'completed', 'cleanup': {'state': 'queued'}}}
            manager.rollback.return_value = {'job': {'status': 'rolled_back'}}
            manager.cancel.return_value = {'job': {'status': 'running', 'cancel_requested': True}}
            with patch.object(router, '_manager', return_value=manager), \
                 patch('utils.auth_config.AuthConfigManager.is_auth_enabled', return_value=True), \
                 patch.object(admission, 'infinidysk_namespace_migration_active', return_value=False), \
                 patch.object(admission, 'infinidysk_postgres_migration_active', return_value=False), \
                 patch.object(router.INFINIDYSK_V2_MIGRATION_MANAGER, 'has_blocking_job', return_value=False), \
                 patch.object(router.INFINIDYSK_V2_POSTGRES_MANAGER, 'has_blocking_job', return_value=False):
                client = Client()
                assert client.get('/process/infinidysk-v2/status').status_code == 401
                assert client.get('/process/infinidysk-v2/jobs/' + 'a' * 32 + '/report').status_code == 401
                manager.get_report.assert_not_called()
                assert client.post('/process/infinidysk-v2/start', json={}).status_code == 401
                assert client.post('/process/infinidysk-v2/cancel', json={}).status_code == 401
                assert client.post('/process/infinidysk-v2/cleanup/preview', json={}).status_code == 401
                assert client.post('/process/infinidysk-v2/cleanup', json={}).status_code == 401
                with patch('utils.auth.decode_token', return_value=SimpleNamespace(type='access', sub='example')), patch('utils.auth_config.AuthConfigManager.validate_token_principal', return_value=True):
                    client.headers['Authorization'] = 'Bearer fixture'
                    assert client.get('/process/infinidysk-v2/status').json()['protocol'] == 1
                    report = client.get('/process/infinidysk-v2/jobs/' + 'a' * 32 + '/report')
                    assert report.status_code == 200, report.text
                    assert report.text == 'Native fixture report\\n'
                    assert report.headers['cache-control'] == 'no-store'
                    assert report.headers['x-content-type-options'] == 'nosniff'
                    assert report.headers['content-type'].startswith('text/plain')
                    manager.get_report.assert_called_once_with('a' * 32)
                    capabilities = client.get('/process/capabilities').json()
                    assert capabilities['api_version'] == app.version
                    assert capabilities['infinidysk_v2'] is True
                    assert capabilities['infinidysk_v2_migration_protocol'] == 1
                    assert capabilities['infinidysk_v2_cleanup'] is True
                    assert capabilities['infinidysk_v2_cleanup_protocol'] == 1
                    with patch.object(router, 'get_project_version', return_value='2.23.9'):
                        assert client.post('/process/infinidysk-v2/preview', json={}).status_code == 426
                        assert client.post('/process/infinidysk-v2/postgres/preview', json={}).status_code == 426
                        assert client.post('/process/infinidysk-v2/cleanup', json={}).status_code == 426
                        assert client.post('/process/infinidysk-v2/cancel', json={}).status_code == 426
                        assert client.get('/process/infinidysk-v2/status').status_code == 200
                        manager.preview.assert_not_called()
                    with patch.object(router, 'get_project_version', return_value='2.24.0'):
                        preview = client.post('/process/infinidysk-v2/preview', json={})
                        assert preview.status_code == 200, preview.text
                        assert preview.json()['capacity'] == capacity
                        assert preview.json()['ready'] is False
                        assert client.post('/process/infinidysk-v2/start', json={}).status_code == 200
                        with patch.object(router.INFINIDYSK_V2_MIGRATION_MANAGER, 'has_blocking_job', return_value=True):
                            assert client.post('/process/infinidysk-v2/cancel', json={}).json()['job']['cancel_requested'] is True
                        cleanup_preview = client.post('/process/infinidysk-v2/cleanup/preview', json={'job_id': 'a' * 32})
                        assert cleanup_preview.json()['ready'] is False
                        assert client.post('/process/infinidysk-v2/cleanup', json={}).json()['job']['cleanup']['state'] == 'queued'
                        with patch.object(router.INFINIDYSK_V2_POSTGRES_MANAGER, 'has_blocking_job', return_value=True):
                            assert client.post('/process/infinidysk-v2/start', json={}).status_code == 409
                            assert client.patch('/config/', json={}).status_code == 409
                        token = admission.reserve_infinidysk_external_mutation('API fixture')
                        try:
                            assert client.post('/process/infinidysk-v2/start', json={}).status_code == 409
                        finally:
                            admission.release_infinidysk_external_mutation(token)
                        manager.get_job.side_effect = InfiniDyskV2MigrationError('Invalid retained job identifier.')
                        response = client.get('/process/infinidysk-v2/jobs/invalid')
                        assert response.status_code == 409, response.text
                        assert response.json()['detail'] == 'Invalid retained job identifier.'
                        manager.get_report.side_effect = InfiniDyskV2MigrationError('The native report is unavailable for this job.')
                        report_missing = client.get('/process/infinidysk-v2/jobs/' + 'b' * 32 + '/report')
                        assert report_missing.status_code == 409, report_missing.text
                        assert report_missing.json()['detail'] == 'The native report is unavailable for this job.'
                    paths = app.openapi()['paths']
                    for path in ('/status', '/preview', '/rehearse', '/start', '/jobs/{job_id}', '/jobs/{job_id}/report', '/rollback', '/cancel', '/cleanup/preview', '/cleanup', '/postgres/status', '/postgres/preview', '/postgres/start', '/postgres/cutover', '/postgres/cancel', '/postgres/rollback'):
                        assert '/process/infinidysk-v2' + path in paths, path
                    assert app.version == '2.24.0'
            """)
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
