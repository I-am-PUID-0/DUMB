import unittest
from unittest.mock import patch

from utils import resource_limits


class ResourceLimitTests(unittest.TestCase):
    def test_available_cpu_count_honors_affinity_and_cgroup(self):
        with (
            patch.object(
                resource_limits.multiprocessing, "cpu_count", return_value=128
            ),
            patch.object(
                resource_limits.os,
                "sched_getaffinity",
                return_value={0, 1, 2, 3},
            ),
            patch.object(resource_limits, "_cgroup_cpu_limit", return_value=2),
        ):
            self.assertEqual(resource_limits.available_cpu_count(), 2)

    def test_available_memory_uses_most_conservative_limit(self):
        with (
            patch.object(
                resource_limits,
                "_host_available_memory_bytes",
                return_value=64 * resource_limits.GIB,
            ),
            patch.object(
                resource_limits,
                "_cgroup_available_memory_bytes",
                return_value=6 * resource_limits.GIB,
            ),
        ):
            self.assertEqual(
                resource_limits.available_memory_bytes(), 6 * resource_limits.GIB
            )

    def test_preinstall_workers_scale_but_retain_safety_ceiling(self):
        self.assertEqual(
            resource_limits.preinstall_worker_count(
                20, cpu_count=128, memory_bytes=128 * resource_limits.GIB
            ),
            8,
        )
        self.assertEqual(
            resource_limits.preinstall_worker_count(
                20, cpu_count=4, memory_bytes=3 * resource_limits.GIB
            ),
            1,
        )

    def test_pnpm_concurrency_scales_with_resources(self):
        self.assertEqual(
            resource_limits.pnpm_concurrency(
                cpu_count=128, memory_bytes=128 * resource_limits.GIB
            ),
            (8, 16),
        )
        self.assertEqual(
            resource_limits.pnpm_concurrency(cpu_count=2, memory_bytes=512 * 1024**2),
            (1, 2),
        )


if __name__ == "__main__":
    unittest.main()
