import builtins
import io
import sys
import types
import unittest
from unittest.mock import patch


class FakePsutil(types.ModuleType):
    CONN_LISTEN = "LISTEN"
    CONN_ESTABLISHED = "ESTABLISHED"

    class NoSuchProcess(Exception):
        pass

    class AccessDenied(Exception):
        pass

    class _Process:
        def __init__(self, pid):
            self.pid = pid

        def create_time(self):
            return 123.0

    def __init__(self):
        super().__init__("psutil")
        self.Process = self._Process

    def cpu_count(self, logical=True):
        return 4

    def disk_usage(self, path):
        return types.SimpleNamespace(total=1000, used=250, percent=25.0)


sys.modules["psutil"] = FakePsutil()

from utils import metrics


class FakeProcessHandler:
    processes = {}


class MetricsCollectorHelperTests(unittest.TestCase):
    def setUp(self):
        self.collector = metrics.MetricsCollector(FakeProcessHandler(), logger=None)

    def test_addr_to_tuple_handles_empty_and_socket_address_like_objects(self):
        self.assertIsNone(metrics._addr_to_tuple(None))
        self.assertEqual(
            metrics._addr_to_tuple(types.SimpleNamespace(ip="127.0.0.1", port=8080)),
            ["127.0.0.1", 8080],
        )

    def test_collect_config_ports_reads_top_level_and_env_ports(self):
        ports = self.collector._collect_config_ports(
            {
                "port": 8080,
                "frontend_port": 3000,
                "backend_port": "ignored-string",
                "env": {"PORT": "9000", "WEBDAV_PORT": "not-a-port"},
            }
        )

        self.assertEqual(ports, [3000, 8080, 9000])

    def test_collect_disk_paths_includes_config_dirs_env_paths_and_extra_paths(self):
        config = {
            "config_dir": "/config/service",
            "config_file": "/config/service/config.xml",
            "log_file": "/logs/service/service.log",
            "env": {
                "DATA_PATH": "/data/service",
                "TEMPLATE": "/data/{service}",
                "REL": "relative",
            },
        }

        fake_usage = types.SimpleNamespace(total=1000, used=250, percent=25.0)
        with (
            patch("utils.metrics.os.path.exists", return_value=True),
            patch("utils.metrics.psutil.disk_usage", return_value=fake_usage),
        ):
            result = self.collector._collect_disk_paths(
                config, extra_paths={"/mnt/debrid"}
            )

        paths = {entry["path"] for entry in result["paths"]}
        self.assertEqual(
            paths,
            {"/config/service", "/logs/service", "/data/service", "/mnt/debrid"},
        )
        self.assertEqual(result["used_total"], 250 * 4)

    def test_read_cgroup_key_values_ignores_malformed_and_non_integer_lines(self):
        def fake_open(path, mode="r", *args, **kwargs):
            return io.StringIO("usage_usec 12345\ninvalid\nother nope\nextra 1 2\n")

        with patch.object(builtins, "open", fake_open):
            self.assertEqual(
                self.collector._read_cgroup_key_values("/sys/fs/cgroup/cpu.stat"),
                {"usage_usec": 12345},
            )

    def test_read_cgroup_io_sums_read_and_write_bytes(self):
        def fake_open(path, mode="r", *args, **kwargs):
            return io.StringIO(
                "8:0 rbytes=10 wbytes=20 rios=1 wios=2\n" "8:16 rbytes=5 wbytes=7\n"
            )

        with patch.object(builtins, "open", fake_open):
            self.assertEqual(
                self.collector._read_cgroup_io(),
                {"read_bytes": 15, "write_bytes": 27},
            )

    def test_read_cgroup_int_supports_max_and_invalid_values(self):
        def fake_open_max(path, mode="r", *args, **kwargs):
            return io.StringIO("max")

        with patch.object(builtins, "open", fake_open_max):
            self.assertEqual(
                self.collector._read_cgroup_int("memory.max", allow_max=True), "max"
            )
            self.assertIsNone(
                self.collector._read_cgroup_int("memory.max", allow_max=False)
            )

        def fake_open_int(path, mode="r", *args, **kwargs):
            return io.StringIO("42")

        with patch.object(builtins, "open", fake_open_int):
            self.assertEqual(self.collector._read_cgroup_int("memory.current"), 42)


if __name__ == "__main__":
    unittest.main()
