import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import defusedxml.ElementTree as ET

from utils import jellyfin_settings


class JellyfinSettingsTests(unittest.TestCase):
    def test_missing_network_xml_is_created_with_requested_port(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(
                jellyfin_settings.CONFIG_MANAGER,
                "get",
                return_value={"config_dir": temp_dir, "port": 18096},
            ):
                updated, error = jellyfin_settings.patch_jellyfin_config()

            path = Path(temp_dir) / "config" / "network.xml"
            root = ET.parse(path).getroot()

        self.assertTrue(updated, error)
        self.assertEqual("18096", root.findtext("InternalHttpPort"))
        self.assertEqual("18096", root.findtext("PublicHttpPort"))


if __name__ == "__main__":
    unittest.main()
