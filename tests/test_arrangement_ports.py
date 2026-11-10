"""The wildcard port suitability probe must never become an exposed listener."""

import unittest
from unittest.mock import MagicMock, Mock, patch

from utils.arrangement_ports import _available


class PortAvailabilityTests(unittest.TestCase):
    def test_wildcard_probe_closes_without_listening_or_handling_traffic(self):
        manager = MagicMock()
        probe = Mock()
        manager.__enter__.return_value = probe
        with patch("utils.arrangement_ports.socket.socket", return_value=manager):
            self.assertTrue(_available(13378))
        probe.bind.assert_called_once_with(("0.0.0.0", 13378))
        for name in ("listen", "accept", "connect", "recv", "send"):
            getattr(probe, name).assert_not_called()
        manager.__exit__.assert_called_once()

    def test_occupied_port_still_closes_and_reports_unavailable(self):
        manager = MagicMock()
        probe = Mock()
        probe.bind.side_effect = OSError("fixture occupied port")
        manager.__enter__.return_value = probe
        with patch("utils.arrangement_ports.socket.socket", return_value=manager):
            self.assertFalse(_available(13378))
        probe.listen.assert_not_called()
        manager.__exit__.assert_called_once()
