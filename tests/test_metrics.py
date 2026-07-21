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

    def test_collect_filesystem_metrics_keeps_selected_path_order(self):
        fake_usage = types.SimpleNamespace(total=1000, used=250, free=750, percent=25.0)
        fake_inode = {
            "total": 100,
            "used": 10,
            "free": 90,
            "percent": 10.0,
            "path": "/data",
        }
        with (
            patch("utils.metrics.psutil.disk_usage", return_value=fake_usage),
            patch.object(
                self.collector, "_collect_inode_usage", return_value=fake_inode
            ),
            patch(
                "utils.metrics._read_mount_entries",
                return_value=[
                    {"mount_point": "/", "fs_type": "overlay"},
                    {"mount_point": "/data", "fs_type": "xfs"},
                ],
            ),
            patch("utils.metrics.os.path.exists", return_value=True),
            patch("utils.metrics.os.path.realpath", side_effect=lambda path: path),
        ):
            result = self.collector._collect_filesystem_metrics(
                ["/data", "/config", "/data"]
            )

        self.assertEqual([entry["path"] for entry in result], ["/data", "/config"])
        self.assertEqual(result[0]["mount_point"], "/data")
        self.assertEqual(result[0]["fs_type"], "xfs")
        self.assertEqual(result[0]["free"], 750)
        disk, inode = self.collector._primary_filesystem_aliases(result)
        self.assertEqual(disk["path"], "/data")
        self.assertEqual(inode["percent"], 10.0)

    def test_normalize_filesystem_paths_falls_back_to_root(self):
        self.assertEqual(metrics._normalize_filesystem_paths([]), ["/"])
        self.assertEqual(
            metrics._normalize_filesystem_paths(
                [" /data/ ", "relative", "/data", None]
            ),
            ["/data"],
        )

    def test_collect_network_metrics_supports_specific_interfaces(self):
        counters = {
            "eth0": types.SimpleNamespace(
                bytes_sent=100,
                bytes_recv=200,
                packets_sent=3,
                packets_recv=4,
                errin=0,
                errout=1,
                dropin=2,
                dropout=0,
            ),
            "lo": types.SimpleNamespace(
                bytes_sent=50,
                bytes_recv=50,
                packets_sent=1,
                packets_recv=1,
                errin=0,
                errout=0,
                dropin=0,
                dropout=0,
            ),
        }
        stats = {
            "eth0": types.SimpleNamespace(isup=True, speed=1000, mtu=1500),
            "lo": types.SimpleNamespace(isup=True, speed=0, mtu=65536),
        }
        with (
            patch(
                "utils.metrics.psutil.net_io_counters",
                return_value=counters,
                create=True,
            ),
            patch("utils.metrics.psutil.net_if_stats", return_value=stats, create=True),
        ):
            aggregate, interfaces = self.collector._collect_network_metrics(
                ["eth0", "missing"]
            )

        self.assertEqual([entry["name"] for entry in interfaces], ["eth0", "missing"])
        self.assertEqual(aggregate["sent_bytes"], 100)
        self.assertEqual(aggregate["recv_bytes"], 200)
        self.assertEqual(interfaces[0]["speed_mbps"], 1000)
        self.assertFalse(interfaces[1]["available"])

    def test_normalize_network_interfaces_all_overrides_specific_names(self):
        self.assertEqual(metrics._normalize_network_interfaces([]), ["all"])
        self.assertEqual(
            metrics._normalize_network_interfaces(["eth0", " all ", "lo"]),
            ["all"],
        )
        self.assertEqual(
            metrics._normalize_network_interfaces(["eth0", "eth0", "lo"]),
            ["eth0", "lo"],
        )

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
