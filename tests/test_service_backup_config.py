"""Real configuration migration/persistence with no live singleton construction."""

from pathlib import Path
import subprocess
import sys
import textwrap
import unittest


class ServiceBackupConfigTests(unittest.TestCase):
    def test_legacy_defaults_and_service_selection_survive_real_config_roundtrip(self):
        code = textwrap.dedent("""
            import ast
            import copy
            import json
            from pathlib import Path
            import tempfile
            from unittest.mock import patch
            from jsonschema import Draft7Validator

            # Execute the actual configuration module except its singleton
            # assignment. Every constructor/migration/prune/save method remains
            # the production implementation, while /config is never opened.
            source = Path('utils/config_loader.py')
            tree = ast.parse(source.read_text(), filename=str(source))
            tree.body = [node for node in tree.body if not (
                isinstance(node, ast.Assign) and any(
                    isinstance(target, ast.Name) and target.id == 'CONFIG_MANAGER'
                    for target in node.targets
                )
            )]
            namespace = {'__name__': 'service_backup_config_fixture', '__file__': str(source.resolve())}
            exec(compile(tree, str(source), 'exec'), namespace)
            ConfigManager = namespace['ConfigManager']
            namespace['load_dotenv'] = lambda *_args, **_kwargs: False
            namespace['find_dotenv'] = lambda *_args, **_kwargs: ''
            defaults_path = Path('utils/dumb_config.json')
            schema_path = Path('utils/dumb_config_schema.json')
            originals = {path: path.read_bytes() for path in (source, defaults_path, schema_path)}
            defaults = json.loads(originals[defaults_path])
            schema = json.loads(originals[schema_path])
            legacy = copy.deepcopy(defaults)
            legacy['installation_backup'].pop('scope')
            legacy['installation_backup'].pop('service_ids')
            legacy['installation_backup'].update(enabled=False, interval_hours=72, keep_last=4)
            legacy['tz'] = 'Etc/UTC'
            legacy['dumb']['ui']['sidebar']['service_search'] = 'reserved retained search'
            legacy['dumb']['github_username'] = 'reserved-fixture-operator'
            legacy['sonarr']['instances']['Default']['env']['RETAIN_NATIVE_SETTING'] = 'fixture-value'
            unaffected = {key: value for key, value in legacy.items() if key != 'installation_backup'}
            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / 'dumb_config.json'
                path.write_text(json.dumps(legacy))
                with patch.object(ConfigManager, '_get_env_var', return_value=None):
                    manager = ConfigManager(file_path=str(path))
                    assert manager.file_path == str(path)
                    assert manager.config['installation_backup']['scope'] == 'installation'
                    assert manager.config['installation_backup']['service_ids'] == []
                    assert manager.config['installation_backup']['interval_hours'] == 72
                    assert manager.config['installation_backup']['keep_last'] == 4
                    assert {key: value for key, value in manager.config.items() if key != 'installation_backup'} == unaffected
                    assert Path(str(path) + '.bak').is_file()
                    manager.config['installation_backup'].update(scope='services', service_ids=['sonarr:Default', 'radarr:Default'])
                    manager.save_config()
                    reread = ConfigManager(file_path=str(path))
                    assert reread.config['installation_backup'] == manager.config['installation_backup']
                    assert {key: value for key, value in reread.config.items() if key != 'installation_backup'} == unaffected
                    assert not list(Draft7Validator(schema).iter_errors(reread.config))
                    disk = json.loads(path.read_text())
                    assert disk['installation_backup']['scope'] == 'services'
                    assert disk['installation_backup']['service_ids'] == ['sonarr:Default', 'radarr:Default']
            assert all(path.read_bytes() == original for path, original in originals.items())
            """)
        result = subprocess.run(
            [sys.executable, "-B", "-c", code],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_service_schedule_schema_enforces_scope_selection_types_and_bounds(self):
        code = textwrap.dedent("""
            import copy
            import json
            from pathlib import Path
            from jsonschema import Draft7Validator
            defaults = json.loads(Path('utils/dumb_config.json').read_text())
            schema = json.loads(Path('utils/dumb_config_schema.json').read_text())
            validator = Draft7Validator(schema)
            assert defaults['installation_backup']['scope'] == 'installation'
            assert defaults['installation_backup']['service_ids'] == []
            maximum = ['fixture:instance' + str(index) for index in range(64)]
            for selection in ([], ['sonarr:Default'], maximum, ['x' * 128]):
                candidate = copy.deepcopy(defaults)
                candidate['installation_backup'].update(scope='services', service_ids=selection)
                assert not list(validator.iter_errors(candidate))
            for field, value in (
                ('scope', 'unknown'), ('scope', None), ('service_ids', 'sonarr:Default'),
                ('service_ids', [None]), ('service_ids', [True]), ('service_ids', ['']),
                ('service_ids', ['x' * 129]), ('service_ids', ['fixture'] * 2),
                ('service_ids', maximum + ['fixture:overflow']),
            ):
                candidate = copy.deepcopy(defaults)
                candidate['installation_backup'][field] = value
                assert list(validator.iter_errors(candidate)), field
            for field in ('scope', 'service_ids'):
                candidate = copy.deepcopy(defaults)
                candidate['installation_backup'].pop(field)
                assert list(validator.iter_errors(candidate)), field
            """)
        result = subprocess.run(
            [sys.executable, "-B", "-c", code],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
