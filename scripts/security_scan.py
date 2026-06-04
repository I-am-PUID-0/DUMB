#!/usr/bin/env python3
from __future__ import annotations

import argparse
import math
import os
from dataclasses import dataclass
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class SecretFinding:
    path: Path
    line_no: int
    line: str
    key: str
    value: str


IGNORED_DIRS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "node_modules",
    "dist",
    "build",
    ".idea",
    ".vscode",
}

IGNORED_ROOT_DIRS = {
    "config",
    "data",
    "log",
    "logs",
}

SCAN_EXTENSIONS = {
    ".py",
    ".pyi",
    ".toml",
    ".yml",
    ".yaml",
    ".json",
    ".ini",
    ".cfg",
    ".env",
    ".sh",
}

PLACEHOLDER_MARKERS = (
    "changeme",
    "change_me",
    "example",
    "placeholder",
    "your_",
    "replace_me",
    "todo",
    "dummy",
    "redacted",
    "********",
    "xxxxxxxx",
)

SUGGESTED_SECRET_KEYWORDS = (
    "api_key",
    "apikey",
    "secret_key",
    "secret",
    "token",
    "access_token",
    "refresh_token",
    "auth_token",
    "bearer_token",
    "private_key",
    "password",
)

ASSIGNMENT_PATTERNS = [
    re.compile(
        r"(?i)^[\t ]*(?:export\s+)?([A-Za-z0-9_]+)\s*[:=]\s*([\"'])([^\"'\\n]*)\2"
    ),
    re.compile(r"(?i)^[\t ]*(?:export\s+)?([A-Za-z0-9_]+)\s*[:=]\s*([^#\s][^#\n]*)"),
    re.compile(r"(?i)^[\t ]*([A-Za-z0-9_]+)\s*:\s*([\"']?)([^#\"'\\n]+)\2"),
]


# Candidate values in real config/ENV files usually avoid spaces.
_LITERAL_TOKEN_RE = re.compile(r"[A-Za-z0-9_.@:/+\-]+")


def _iter_candidate_files(root: Path):
    for current, dirs, files in os.walk(root):
        current_path = Path(current)
        try:
            relative_parts = current_path.relative_to(root).parts
        except ValueError:
            relative_parts = current_path.parts
        if relative_parts and relative_parts[0] in IGNORED_ROOT_DIRS:
            dirs[:] = []
            continue

        dirs[:] = [
            name
            for name in dirs
            if name not in IGNORED_DIRS and not name.startswith(".")
        ]

        for name in files:
            path = Path(current) / name
            if path.suffix.lower() not in SCAN_EXTENSIONS:
                continue
            if path.name.startswith(".") and not path.name.startswith(".env"):
                continue
            if any(part in IGNORED_DIRS for part in path.parts):
                continue
            yield path


def _is_placeholder_like(value: str) -> bool:
    lowered = value.lower().strip()
    if not lowered:
        return True
    if len(lowered) <= 3:
        return True
    if any(marker in lowered for marker in PLACEHOLDER_MARKERS):
        return True
    if lowered in {"none", "null", "nil", "true", "false", "yes", "no", "localhost"}:
        return True
    if lowered in {"0", "1", "test", "sample", "example", "dev", "debug"}:
        return True
    if all(ch in "0123456789" for ch in lowered):
        return True
    return False


def _entropy(text: str) -> float:
    if not text:
        return 0.0
    symbols: dict[str, int] = {}
    for ch in text:
        symbols[ch] = symbols.get(ch, 0) + 1
    probs = [count / len(text) for count in symbols.values()]
    return -sum(p * math.log2(p) for p in probs)


def _looks_like_secret_literal(value: str) -> bool:
    candidate = value.strip().strip("\"''")

    if not candidate:
        return False

    if len(candidate) < 24:
        return False

    if _is_placeholder_like(candidate):
        return False

    if candidate.lower().endswith("\n"):
        return False

    # Skip expressions/function calls and inline assignments.
    if any(ch in candidate for ch in "()[]{}=><"):
        return False

    # Avoid prose and comment fragments with spaces.
    if " " in candidate:
        return False

    # Restrict to token-like values to reduce false positives.
    if not _LITERAL_TOKEN_RE.fullmatch(candidate):
        return False

    # Require either sufficient entropy or an expected secret-like length/shape.
    if len(candidate) >= 24 and _entropy(candidate) >= 3.2:
        return True

    if len(candidate) >= 32 and any(ch.isdigit() for ch in candidate):
        return True

    return False


def _is_secret_key_name(key: str) -> bool:
    key_name = key.lower()
    return any(keyword in key_name for keyword in SUGGESTED_SECRET_KEYWORDS)


def scan_content_for_secrets(text: str, path: Path) -> list[SecretFinding]:
    findings = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        for pattern in ASSIGNMENT_PATTERNS:
            match = pattern.match(line.strip())
            if not match:
                continue

            groups = match.groups()
            if len(groups) == 3:
                key, _quote, value = groups
            else:
                key, value = groups

            if not _is_secret_key_name(key):
                break

            if _looks_like_secret_literal(value):
                findings.append(
                    SecretFinding(
                        path=path,
                        line_no=line_no,
                        line=line.rstrip("\n"),
                        key=key,
                        value=value.strip(),
                    )
                )
            break

    return findings


def find_secrets(root: Path) -> list[SecretFinding]:
    results = []
    for path in _iter_candidate_files(root):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except (OSError, UnicodeDecodeError):
            continue

        findings = scan_content_for_secrets(text, path)
        results.extend(findings)
    return results


def _format_finding(finding: SecretFinding) -> str:
    return (
        f"{finding.path.relative_to(ROOT)}:{finding.line_no}:"
        f" possible secret in {finding.key}: {finding.line.strip()}"
    )


def run_scan(root: Path) -> list[SecretFinding]:
    findings = find_secrets(root)
    for finding in findings:
        print(_format_finding(finding))
    return findings


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Basic secrets scan for suspicious literals"
    )
    parser.add_argument(
        "--root",
        default=str(ROOT),
        help="Project root to scan (default: repository root)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = Path(args.root).resolve()
    findings = run_scan(root)
    if findings:
        print(f"Detected {len(findings)} potential secret matches.")
        return 1
    print("No obvious hard-coded secrets detected.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
