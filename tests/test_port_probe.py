import errno
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

from utils import port_probe


class PortProbeTests(unittest.TestCase):
    def test_rejects_invalid_ports_without_opening_a_socket(self):
        with patch.object(port_probe.socket, "socket") as socket_factory:
            for value in (None, True, 0, -1, 65536, "5572"):
                self.assertFalse(port_probe.is_port_available(value))
        socket_factory.assert_not_called()

    def test_existing_listener_marks_port_unavailable(self):
        listener = SimpleNamespace(
            status=port_probe.psutil.CONN_LISTEN,
            laddr=SimpleNamespace(port=5572),
        )
        with (
            patch.object(port_probe.psutil, "net_connections", return_value=[listener]),
            patch.object(port_probe, "_check_bind") as check_bind,
        ):
            self.assertFalse(port_probe.is_port_available(5572))
        check_bind.assert_not_called()

    def test_checks_both_loopback_families_without_listening(self):
        with (
            patch.object(port_probe.psutil, "net_connections", return_value=[]),
            patch.object(port_probe, "_check_bind", side_effect=[True, None]) as check,
        ):
            self.assertTrue(port_probe.is_port_available(5572))
        self.assertEqual(
            check.call_args_list,
            [
                call(port_probe.socket.AF_INET, "127.0.0.1", 5572),
                call(port_probe.socket.AF_INET6, "::1", 5572),
            ],
        )

    def test_bind_conflict_is_unavailable_and_socket_is_closed(self):
        sock = MagicMock()
        sock.bind.side_effect = OSError(errno.EADDRINUSE, "in use")
        with patch.object(port_probe.socket, "socket", return_value=sock):
            self.assertFalse(
                port_probe._check_bind(
                    port_probe.socket.AF_INET,
                    "127.0.0.1",
                    5572,
                )
            )
        sock.listen.assert_not_called()
        sock.accept.assert_not_called()
        sock.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
