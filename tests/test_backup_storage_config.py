"""Registered destinations survive real config merge, validation and pruning."""

from pathlib import Path
import subprocess
import sys
import textwrap
import unittest


class BackupStorageConfigTests(unittest.TestCase):
    def test_new_empty_preference_preserves_preexisting_exact_rollback_receipt(self):
        code = textwrap.dedent("""
            import copy
            from types import SimpleNamespace
            from unittest.mock import Mock
            from utils.infinidysk_v2_migration import InfiniDyskV2MigrationManager, InfiniDyskV2MigrationError, _digest
            current={'infinidysk':{'runtime_generation':'2','process_name':'InfiniDysk'}}
            original=copy.deepcopy(current)
            original['infinidysk']['runtime_generation']='1'
            config=SimpleNamespace(config=copy.deepcopy(current),save_config=Mock())
            manager=InfiniDyskV2MigrationManager(config_manager=config)
            plan={'owned_config_paths':[['infinidysk']],'config_snapshot':original,'source':{'process_name':'InfiniDysk'}}
            job={'plan':plan,'activation_fingerprint':_digest(manager._owned(current,plan))}
            config.config['infinidysk']['backup_destination_id']=''
            manager._restore_config(job)
            assert config.config['infinidysk']['runtime_generation']=='1'
            for changes in ({'backup_destination_id':'nas'},{'backup_destination_id':'','mount_path':'/changed'}):
                config.config=copy.deepcopy(current)
                config.config['infinidysk'].update(changes)
                try:
                    manager._restore_config(job)
                except InfiniDyskV2MigrationError:
                    pass
                else:
                    raise AssertionError('A selected destination or unrelated config edit bypassed exact rollback ownership')
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

    def test_opt_in_defaults_and_named_profiles_round_trip(self):
        code = textwrap.dedent("""
            import copy
            import json
            from pathlib import Path
            import tempfile
            from jsonschema import Draft7Validator
            from utils.config_loader import ConfigManager
            defaults=json.loads(Path('utils/dumb_config.json').read_text())
            schema=json.loads(Path('utils/dumb_config_schema.json').read_text())
            services=('cli_debrid','decypharr','infinidysk','riven_backend')
            assert defaults['backup_storage']=={'default_destination_id':'','profiles':{}}
            assert all(defaults[key]['backup_destination_id']=='' for key in services)
            original=copy.deepcopy(defaults)
            original.pop('backup_storage')
            for key in services:
                original[key].pop('backup_destination_id')
            with tempfile.TemporaryDirectory() as folder:
                path=Path(folder)/'dumb_config.json'
                path.write_text(json.dumps(original))
                manager=ConfigManager(file_path=str(path))
                assert manager.config['backup_storage']==defaults['backup_storage']
                manager.config['backup_storage']={'default_destination_id':'nas','profiles':{'nas':{'name':'Cold archives','path':'/backup-volume','enabled':True}}}
                manager.config['infinidysk']['backup_destination_id']='@default'
                manager.save_config()
                reread=ConfigManager(file_path=str(path))
                assert reread.config['backup_storage']==manager.config['backup_storage']
                assert reread.config['infinidysk']['backup_destination_id']=='@default'
                candidate=copy.deepcopy(reread.config)
                candidate['backup_storage']['profiles']['../escape']=candidate['backup_storage']['profiles'].pop('nas')
                assert list(Draft7Validator(schema).iter_errors(candidate))
                candidate=copy.deepcopy(reread.config)
                candidate['backup_storage']['profiles']['nas']['enabled']='false'
                assert list(Draft7Validator(schema).iter_errors(candidate))
                candidate=copy.deepcopy(reread.config)
                candidate['infinidysk']['backup_destination_id']='../escape'
                assert list(Draft7Validator(schema).iter_errors(candidate))
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
