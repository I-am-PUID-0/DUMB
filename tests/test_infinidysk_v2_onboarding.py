"""Native onboarding preserves admitted delivery ownership before any writes."""

from pathlib import Path
import subprocess
import sys
import textwrap
import unittest


class NativeOnboardingOwnershipTests(unittest.TestCase):
    def test_actual_startup_preserves_owner_and_rejects_unsafe_topology(self):
        code = textwrap.dedent("""
            import copy
            import json
            from pathlib import Path
            from unittest.mock import Mock, patch
            from fastapi import HTTPException
            from api.routers import process as router

            defaults = Path('utils/dumb_config.json')
            config = json.loads(defaults.read_text())
            config['infinidysk'].update(
                enabled=True, runtime_generation='2', mount_backend='rclone',
                mount_path='/mnt/debrid/retained',
            )
            owner = {
                'enabled': True, 'core_service': 'infinidysk',
                'process_name': 'Retained delivery owner', 'key_type': 'infinidysk',
                'mount_dir': '/mnt/debrid', 'mount_name': 'retained',
                'auto_update': False,
            }
            config['rclone']['instances'] = {'Custom owner': copy.deepcopy(owner)}
            service = config['infinidysk']
            assert router._native_infinidysk_rclone_owner(
                service, config['rclone']['instances']) == 'Custom owner'
            assert router._native_infinidysk_rclone_owner(
                {'runtime_generation': '1'}, {}) is None

            unsafe = [
                {},
                {'Custom owner': {**owner, 'enabled': False}},
                {'One': owner, 'Two': {**owner, 'process_name': 'Second owner'}},
                {'Wrong path': {**owner, 'mount_name': 'other'}},
                {'Custom owner': owner, 'Foreign': {
                    **owner, 'core_service': 'another', 'key_type': 'another',
                }},
            ]
            for instances in unsafe:
                before = copy.deepcopy(instances)
                try:
                    router._native_infinidysk_rclone_owner(service, instances)
                except HTTPException as error:
                    assert error.status_code == 409
                else:
                    raise AssertionError('Unsafe native owner was admitted')
                assert instances == before
            assert router._native_infinidysk_rclone_owner(
                {**service, 'mount_backend': 'fuse'}, {}) is None

            manager = Mock(config=config, default_config_path=str(defaults))
            manager.get.side_effect = lambda key, default=None: config.get(key, default)
            request = router.UnifiedStartRequest(
                core_services=[{
                    'name': 'infinidysk',
                    'service_options': {
                        'rclone': {'mount_name': 'replacement'},
                        'Custom owner': {'mount_name': 'replacement'},
                    },
                }],
                optional_services=[],
            )
            updater = Mock()
            started = set()
            updater.auto_update.side_effect = lambda name, **kwargs: (
                started.add(name) or object(), None
            )
            with patch.object(router, 'CONFIG_MANAGER', manager), \
                 patch.object(router, '_validate_infinidysk_service_options'), \
                 patch.object(router, '_materialize_onboarding_resources') as materialize, \
                 patch.object(router, 'infinidysk_namespace_migration_active', return_value=False), \
                 patch.object(router, 'infinidysk_postgres_migration_active', return_value=False), \
                 patch.object(router, 'wait_for_process_running', side_effect=lambda state, name, **kwargs: name in started), \
                 patch('utils.nzbdav_settings.patch_nzbdav_config', return_value=False):
                result = router._run_startup_admitted(request, updater, Mock(), Mock())
                assert result['errors'] == [], result
                assert set(config['rclone']['instances']) == {'Custom owner'}
                assert config['rclone']['instances']['Custom owner'] == owner
                assert [call.args[0] for call in updater.auto_update.call_args_list] == [
                    config['infinidysk']['process_name'], 'Retained delivery owner',
                ], updater.auto_update.call_args_list
                materialize.assert_called_once()
                materialize.reset_mock()
                manager.save_config.reset_mock()
                config['rclone']['instances'] = {}
                try:
                    router._run_startup_admitted(request, updater, Mock(), Mock())
                except HTTPException as error:
                    assert error.status_code == 409
                else:
                    raise AssertionError('Missing owner was materialized')
                materialize.assert_not_called()
                manager.save_config.assert_not_called()

            with patch.object(router, 'infinidysk_namespace_migration_active', return_value=False), \
                 patch.object(router, 'infinidysk_postgres_migration_active', return_value=False), \
                 patch.object(router, 'infinidysk_v2_jobs_active', return_value=True), \
                 patch.object(router, '_run_startup_admitted') as startup:
                try:
                    router._run_startup(
                        router.UnifiedStartRequest(core_services=[], optional_services=['audiobookshelf']),
                        updater, Mock(), Mock(),
                    )
                except HTTPException as error:
                    assert error.status_code == 409
                else:
                    raise AssertionError('Setup raced an active native job')
                startup.assert_not_called()
        """)
        result = subprocess.run(
            [sys.executable, "-B", "-c", code],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            timeout=45,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
