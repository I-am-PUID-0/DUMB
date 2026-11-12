"""Exercise the actual final-stage OpenCL install command without host installs."""

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = {
    "ocl-icd-libopencl1",
    "clinfo",
    "intel-opencl-icd",
    "intel-opencl-icd-legacy",
}


def opencl_runtime_command():
    source = ROOT.joinpath("Dockerfile").read_text()
    marker = source.index("# Intel OpenCL is an image dependency")
    before, block = source[:marker], source[marker:]
    block = block.split("# Copy artifacts from builder stages", 1)[0]
    run = re.search(r"^RUN .*?\\\n", block, flags=re.MULTILINE)
    if run is None:
        raise AssertionError("The runtime install RUN command is unavailable.")
    return before, block[run.end() :].replace("\\\n", " ")


class OpenClImagePackagingTests(unittest.TestCase):
    def exercise(self, architecture, *, fail_update=False):
        _, command = opencl_runtime_command()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tools = root / "bin"
            tools.mkdir()
            log = root / "commands.jsonl"
            script = (
                "#!" + sys.executable + " -B\n"
                "import json,os,sys\n"
                "from pathlib import Path\n"
                "name=Path(sys.argv[0]).name\n"
                "with open(os.environ['OPENCL_TEST_LOG'],'a') as handle:\n"
                " handle.write(json.dumps([name,sys.argv[1:]])+'\\n')\n"
                "if name=='dpkg':\n"
                " assert sys.argv[1:]==['--print-architecture']\n"
                " print(os.environ['OPENCL_TEST_ARCH'])\n"
                "if name=='apt-get' and sys.argv[-1]=='update' and os.environ.get('OPENCL_TEST_FAIL_UPDATE')=='1':\n"
                " sys.exit(42)\n"
            )
            for name in ("dpkg", "apt-get", "dpkg-query", "rm"):
                target = tools / name
                target.write_text(script)
                target.chmod(0o755)
            environment = dict(os.environ)
            environment.update(
                PATH=str(tools),
                OPENCL_TEST_LOG=str(log),
                OPENCL_TEST_ARCH=architecture,
                OPENCL_TEST_FAIL_UPDATE="1" if fail_update else "0",
                # The base image's architecture, not a caller-supplied hint,
                # must select whether Intel packages can be installed.
                TARGETARCH="arm64" if architecture == "amd64" else "amd64",
            )
            completed = subprocess.run(
                ["/bin/sh", "-c", command],
                env=environment,
                capture_output=True,
                text=True,
                timeout=10,
            )
            rows = [json.loads(line) for line in log.read_text().splitlines()]
            return completed, rows

    def test_amd64_installs_current_and_legacy_drivers_without_recommends(self):
        result, rows = self.exercise("amd64")
        self.assertEqual(result.returncode, 0, result.stderr)
        installs = [
            args for name, args in rows if name == "apt-get" and "install" in args
        ]
        self.assertEqual(len(installs), 1)
        self.assertIn("--no-install-recommends", installs[0])
        self.assertEqual(set(installs[0]) & PACKAGES, PACKAGES)
        query = next(args for name, args in rows if name == "dpkg-query")
        self.assertTrue(PACKAGES.issubset(query))
        update = next(
            args for name, args in rows if name == "apt-get" and args[-1] == "update"
        )
        self.assertIn("APT::Update::Error-Mode=any", update)

    def test_arm64_never_installs_or_refreshes_intel_packages(self):
        result, rows = self.exercise("arm64")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(rows, [["dpkg", ["--print-architecture"]]])

    def test_repository_refresh_failure_does_not_report_a_successful_image(self):
        result, rows = self.exercise("amd64", fail_update=True)
        self.assertEqual(result.returncode, 42)
        self.assertFalse(
            any(name == "apt-get" and "install" in args for name, args in rows)
        )
        self.assertFalse(any(name == "dpkg-query" for name, _ in rows))

    def test_opencl_packages_belong_only_to_the_final_runtime_stage(self):
        before, _ = opencl_runtime_command()
        final_stage = before.rsplit("FROM ", 1)[1].splitlines()[0]
        self.assertEqual(final_stage, "base AS final-stage")
        self.assertNotIn("intel-opencl-icd", before)


if __name__ == "__main__":
    unittest.main()
