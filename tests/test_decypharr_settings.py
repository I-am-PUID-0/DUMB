import sys
import types
import unittest

fastapi_stub = sys.modules.get("fastapi")
if fastapi_stub is not None and not hasattr(fastapi_stub, "WebSocket"):
    fastapi_stub.WebSocket = object
elif fastapi_stub is None:
    fastapi_stub = types.ModuleType("fastapi")
    fastapi_stub.WebSocket = object
    sys.modules["fastapi"] = fastapi_stub

from utils.decypharr_settings import _uses_combined_root


class DecypharrSettingsTests(unittest.TestCase):
    def test_combined_root_requires_decypharr_plus_companion_workflow(self):
        self.assertFalse(_uses_combined_root(["decypharr"]))
        self.assertFalse(_uses_combined_root(["nzbdav", "altmount"]))
        self.assertTrue(_uses_combined_root(["decypharr", "nzbdav"]))
        self.assertTrue(_uses_combined_root(["decypharr", "altmount"]))
        self.assertTrue(_uses_combined_root(["Decypharr", " AltMount "]))


if __name__ == "__main__":
    unittest.main()
