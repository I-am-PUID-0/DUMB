import unittest
from pathlib import Path


class RegressionInstallOnlyServiceTests(unittest.TestCase):
    def test_zurg_validator_checks_two_executable_architecture_digests(self):
        source = Path("scripts/regression_install_only_service.py").read_text(
            encoding="utf-8"
        )

        self.assertEqual(2, source.count("zurg_setup(install_only=True)"))
        self.assertEqual(2, source.count('["readelf", "-h", str(binary)]'))
        self.assertIn("if prior_digest == latest_digest:", source)
        self.assertIn('instance["release_version_enabled"] = False', source)


if __name__ == "__main__":
    unittest.main()
