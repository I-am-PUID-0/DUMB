"""Fixed, resource-limited decoder for explicitly authorized workflow samples."""

import os
import resource
import sys


def main():
    resource.setrlimit(resource.RLIMIT_CPU, (5, 5))
    resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024,) * 2)
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    if os.geteuid() == 0:
        os.setgroups([])
        os.setgid(65534)
        os.setuid(65534)
    os.chdir("/tmp")
    binary = "/usr/bin/ffmpeg"
    mode = "audio" if sys.argv[1:] == ["audio"] else "video"
    stream = ["-vn", "-frames:a", "1"] if mode == "audio" else ["-an", "-frames:v", "1"]
    os.execv(
        binary,
        [
            binary,
            "-nostdin",
            "-v",
            "error",
            "-xerror",
            "-threads",
            "1",
            "-filter_threads",
            "1",
            "-protocol_whitelist",
            "pipe",
            "-i",
            "pipe:0",
            *stream,
            "-threads",
            "1",
            "-f",
            "framemd5",
            "pipe:1",
        ],
    )


if __name__ == "__main__":
    main()
