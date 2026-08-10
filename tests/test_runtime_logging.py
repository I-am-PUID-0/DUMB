import logging
import types
import unittest
from unittest.mock import patch

from utils import runtime_logging


class RuntimeLoggingTests(unittest.TestCase):
    def setUp(self):
        self.logger = logging.getLogger("DUMB-runtime-log-level-test")
        self.original_logger_level = self.logger.level
        self.original_propagate = self.logger.propagate
        self.logger.propagate = False
        self.uvicorn_levels = {
            name: logging.getLogger(name).level
            for name in runtime_logging._UVICORN_LOGGER_NAMES
        }

    def tearDown(self):
        self.logger.setLevel(self.original_logger_level)
        self.logger.propagate = self.original_propagate
        for name, level in self.uvicorn_levels.items():
            logging.getLogger(name).setLevel(level)
        runtime_logging._runtime_debug_override_active = False

    def test_debug_override_applies_immediately_and_restores_configured_levels(self):
        config = {
            "log_level": "WARNING",
            "api_service": {"log_level": "ERROR"},
        }
        manager = types.SimpleNamespace(get=lambda key: config if key == "dumb" else {})

        with patch.object(runtime_logging, "CONFIG_MANAGER", manager):
            enabled = runtime_logging.set_runtime_debug_logging(True, self.logger)

            self.assertEqual(self.logger.level, logging.DEBUG)
            self.assertTrue(enabled["debug_enabled"])
            self.assertTrue(enabled["override_active"])
            self.assertTrue(enabled["resets_on_restart"])
            for name in runtime_logging._UVICORN_LOGGER_NAMES:
                self.assertEqual(logging.getLogger(name).level, logging.DEBUG)

            disabled = runtime_logging.set_runtime_debug_logging(False, self.logger)

        self.assertEqual(self.logger.level, logging.WARNING)
        self.assertFalse(disabled["debug_enabled"])
        self.assertFalse(disabled["override_active"])
        for name in runtime_logging._UVICORN_LOGGER_NAMES:
            self.assertEqual(logging.getLogger(name).level, logging.ERROR)

    def test_invalid_configured_levels_fall_back_to_info(self):
        manager = types.SimpleNamespace(
            get=lambda key: {
                "log_level": "verbose",
                "api_service": {"log_level": "trace"},
            }
        )

        with patch.object(runtime_logging, "CONFIG_MANAGER", manager):
            state = runtime_logging.set_runtime_debug_logging(False, self.logger)

        self.assertEqual(state["configured_level"], "INFO")
        self.assertEqual(state["configured_uvicorn_level"], "INFO")
        self.assertEqual(state["effective_level"], "INFO")


if __name__ == "__main__":
    unittest.main()
