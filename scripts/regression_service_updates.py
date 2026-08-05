#!/usr/bin/env python3
"""Run the retained DUMB service-update matrix with bounded concurrency."""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

DEFAULT_MATRIX = Path(__file__).with_name("service_update_regression_matrix.json")
DEFAULT_CACHE = Path(__file__).resolve().parents[2] / ".dumb-regression-cache"
_ACTIVE_LOCK = threading.Lock()
_ACTIVE: set[subprocess.Popen] = set()


def load_cases(
    matrix_path: Path,
    *,
    include_pending: bool = False,
    selected_ids: set[str] | None = None,
) -> tuple[list[dict], list[dict]]:
    with matrix_path.open(encoding="utf-8") as handle:
        matrix = json.load(handle)
    cases = matrix.get("cases")
    if matrix.get("schema_version") != 1 or not isinstance(cases, list):
        raise ValueError("Unsupported or malformed regression matrix")

    runnable: list[dict] = []
    skipped: list[dict] = []
    known_ids = {str(case.get("id")) for case in cases if isinstance(case, dict)}
    unknown = (selected_ids or set()) - known_ids
    if unknown:
        raise ValueError(f"Unknown regression case(s): {', '.join(sorted(unknown))}")

    for case in cases:
        if not isinstance(case, dict):
            continue
        case_id = str(case.get("id") or "")
        if selected_ids and case_id not in selected_ids:
            continue
        reason = None
        if not case.get("previous"):
            reason = "no deterministic previous version"
        elif not case.get("qualified") and not include_pending:
            reason = "pending qualification"
        if reason:
            skipped.append({"id": case_id, "reason": reason, "note": case.get("note")})
        else:
            runnable.append(case)
    return runnable, skipped


def case_command(case: dict, args: argparse.Namespace) -> list[str]:
    single_runner = Path(__file__).with_name("regression_service_update.py")
    command = [
        sys.executable,
        str(single_runner),
        "--key",
        str(case["key"]),
        "--previous",
        str(case["previous"]),
        "--selector",
        str(case.get("selector") or "release"),
        "--mode",
        str(case.get("mode") or "update"),
        "--image",
        args.image,
        "--cache-dir",
        str(args.cache_dir),
        "--startup-timeout",
        str(case.get("startup_timeout") or args.startup_timeout),
        "--update-timeout",
        str(case.get("update_timeout") or args.update_timeout),
    ]
    if case.get("instance"):
        command.extend(["--instance", str(case["instance"])])
    if case.get("target_version"):
        command.extend(["--target-version", str(case["target_version"])])
    if case.get("update_target"):
        command.extend(["--update-target", str(case["update_target"])])
    for dependency in case.get("dependencies") or []:
        command.extend(["--enable-dependency", str(dependency)])
    for health_url in case.get("health_urls") or []:
        command.extend(["--health-url", str(health_url)])
    config_overrides = case.get("config_overrides") or {}
    if config_overrides:
        command.extend(
            [
                "--config-overrides-json",
                json.dumps(config_overrides, separators=(",", ":")),
            ]
        )
    if args.keep:
        command.append("--keep")
    return command


def _extract_report(stdout: str) -> dict | None:
    decoder = json.JSONDecoder()
    for index, character in enumerate(stdout):
        if character != "{":
            continue
        try:
            value, end = decoder.raw_decode(stdout[index:])
        except json.JSONDecodeError:
            continue
        if not stdout[index + end :].strip() and isinstance(value, dict):
            return value
    return None


def run_case(case: dict, args: argparse.Namespace, log_dir: Path) -> dict:
    case_id = str(case["id"])
    started = time.monotonic()
    process = subprocess.Popen(
        case_command(case, args),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        env=args.worker_env,
    )
    with _ACTIVE_LOCK:
        _ACTIVE.add(process)
    try:
        stdout, stderr = process.communicate()
    finally:
        with _ACTIVE_LOCK:
            _ACTIVE.discard(process)

    (log_dir / f"{case_id}.stdout.log").write_text(stdout, encoding="utf-8")
    (log_dir / f"{case_id}.stderr.log").write_text(stderr, encoding="utf-8")
    report = _extract_report(stdout)
    result = {
        "id": case_id,
        "key": case.get("key"),
        "instance": case.get("instance"),
        "current_config_enabled": bool(case.get("current_config_enabled")),
        "returncode": process.returncode,
        "duration_seconds": round(time.monotonic() - started, 1),
        "result": "passed" if process.returncode == 0 and report else "failed",
    }
    if report:
        result["report"] = report
    if process.returncode != 0:
        result["error"] = (stderr.strip().splitlines() or ["unknown failure"])[-1]
    print(
        f"[{result['result'].upper():6}] {case_id} ({result['duration_seconds']}s)",
        flush=True,
    )
    return result


def stop_active_workers() -> None:
    with _ACTIVE_LOCK:
        active = list(_ACTIVE)
    for process in active:
        if process.poll() is None:
            try:
                os.killpg(process.pid, 2)
            except ProcessLookupError:
                pass
    deadline = time.monotonic() + 45
    for process in active:
        if process.poll() is not None:
            continue
        try:
            process.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, 15)
            except ProcessLookupError:
                pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix", type=Path, default=DEFAULT_MATRIX)
    parser.add_argument("--jobs", type=int, default=2)
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument("--include-pending", action="store_true")
    parser.add_argument("--image", default="dumb-regression-base:local")
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--report-dir", type=Path)
    parser.add_argument("--startup-timeout", type=int, default=600)
    parser.add_argument("--update-timeout", type=int, default=1800)
    parser.add_argument("--keep", action="store_true")
    parser.add_argument(
        "--use-gh-auth-token",
        action="store_true",
        help="Use the current gh CLI token only inside disposable regression configs",
    )
    args = parser.parse_args()
    if args.jobs < 1:
        parser.error("--jobs must be at least 1")
    return args


def main() -> int:
    args = parse_args()
    repo_root = Path(__file__).resolve().parents[1]
    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_dir = args.report_dir or repo_root / ".regression-reports" / timestamp
    report_dir.mkdir(parents=True, exist_ok=True)
    args.cache_dir.mkdir(parents=True, exist_ok=True)
    args.worker_env = os.environ.copy()
    if args.use_gh_auth_token:
        token = subprocess.run(
            ["gh", "auth", "token"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if not token:
            raise ValueError("gh auth token returned an empty token")
        args.worker_env["DUMB_REGRESSION_GITHUB_TOKEN"] = token
    cases, skipped = load_cases(
        args.matrix,
        include_pending=args.include_pending,
        selected_ids=set(args.case) or None,
    )
    if not cases:
        raise ValueError("No runnable regression cases selected")

    started_at = dt.datetime.now(dt.timezone.utc)
    results: list[dict] = []
    print(f"Running {len(cases)} case(s) with {args.jobs} worker(s).", flush=True)
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs)
    futures = {
        executor.submit(run_case, case, args, report_dir): case for case in cases
    }
    try:
        for future in concurrent.futures.as_completed(futures):
            results.append(future.result())
    except KeyboardInterrupt:
        print("Interrupted; stopping active regression workers...", file=sys.stderr)
        for future in futures:
            future.cancel()
        # Cancel queued work before stopping active workers. Otherwise a worker
        # slot freed during shutdown can start a case the operator explicitly
        # interrupted.
        stop_active_workers()
        executor.shutdown(wait=True, cancel_futures=True)
        raise
    else:
        executor.shutdown(wait=True)
    finally:
        stop_active_workers()

    results.sort(key=lambda item: item["id"])
    report = {
        "schema_version": 1,
        "started_at": started_at.isoformat(),
        "finished_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "workers": args.jobs,
        "image": args.image,
        "cache_dir": str(args.cache_dir),
        "results": results,
        "skipped": skipped,
        "summary": {
            "passed": sum(result["result"] == "passed" for result in results),
            "failed": sum(result["result"] == "failed" for result in results),
            "skipped": len(skipped),
        },
    }
    report_path = report_dir / "report.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"Report: {report_path}")
    print(json.dumps(report["summary"], indent=2))
    return 1 if report["summary"]["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
