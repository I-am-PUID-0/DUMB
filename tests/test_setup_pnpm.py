import unittest
from pathlib import Path


class SetupPnpmTests(unittest.TestCase):
    def test_runtime_install_allows_lockfile_refresh(self):
        setup_source = Path("utils/setup.py").read_text(encoding="utf-8")

        self.assertIn('"--no-frozen-lockfile"', setup_source)
        self.assertIn('"npm_config_frozen_lockfile", "false"', setup_source)


if __name__ == "__main__":
    unittest.main()
