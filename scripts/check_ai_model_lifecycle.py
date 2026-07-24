#!/usr/bin/env python3
"""Check DUMB's model lifecycle catalog against official provider tables."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
import sys
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from utils.ai_model_catalog import (  # noqa: E402
    ANTHROPIC_DEPRECATIONS_URL,
    ANTHROPIC_MODEL_LIFECYCLE,
    GEMINI_DEPRECATIONS_URL,
    GEMINI_MODEL_LIFECYCLE,
    OPENAI_DEPRECATIONS_URL,
    OPENAI_MODEL_LIFECYCLE,
    lifecycle_check_model,
    normalize_model_name,
)


@dataclass
class Cell:
    text_parts: list[str] = field(default_factory=list)
    codes: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return " ".join("".join(self.text_parts).split())


class TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tables: list[list[list[Cell]]] = []
        self._table: list[list[Cell]] | None = None
        self._row: list[Cell] | None = None
        self._cell: Cell | None = None
        self._in_code = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            self._table = []
        elif tag == "tr" and self._table is not None:
            self._row = []
        elif tag in {"th", "td"} and self._row is not None:
            self._cell = Cell()
        elif tag == "code" and self._cell is not None:
            self._in_code = True

    def handle_data(self, data: str) -> None:
        if self._cell is None:
            return
        self._cell.text_parts.append(data)
        if self._in_code and data.strip():
            self._cell.codes.append(data.strip())

    def handle_endtag(self, tag: str) -> None:
        if tag == "code":
            self._in_code = False
        elif tag in {"th", "td"} and self._cell is not None:
            if self._row is not None:
                self._row.append(self._cell)
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._table is not None and self._row:
                self._table.append(self._row)
            self._row = None
        elif tag == "table" and self._table is not None:
            self.tables.append(self._table)
            self._table = None


def _iso_date(value: str) -> str | None:
    normalized = (
        str(value or "")
        .replace("\u2011", "-")
        .replace("\u2013", "-")
        .replace("\u2014", "-")
        .strip()
    )
    if not normalized or "no shutdown date" in normalized.lower():
        return None
    for date_format in ("%Y-%m-%d", "%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(normalized, date_format).date().isoformat()
        except ValueError:
            continue
    return None


def parse_lifecycle_rows(provider: str, html: str) -> list[dict[str, str]]:
    provider = str(provider or "").strip().lower()
    if provider == "openai":
        start = html.find('<h2 id="upcoming-deprecations"')
        end = html.find('<h2 id="past-deprecations"')
        if start < 0 or end <= start:
            raise ValueError("OpenAI deprecations page section markers were not found")
        html = html[start:end]

    parser = TableParser()
    parser.feed(html)
    observed = []
    for table in parser.tables:
        if len(table) < 2:
            continue
        headers = [cell.text.lower() for cell in table[0]]
        shutdown_index = next(
            (
                index
                for index, header in enumerate(headers)
                if header in {"shutdown date", "retirement date"}
            ),
            None,
        )
        if shutdown_index is None:
            continue
        model_index = next(
            (
                index
                for index, header in enumerate(headers)
                if "model" in header and "replacement" not in header
            ),
            None,
        )
        if model_index is None:
            continue
        replacement_index = next(
            (
                index
                for index, header in enumerate(headers)
                if "replacement" in header or "substitute" in header
            ),
            None,
        )
        for row in table[1:]:
            if max(shutdown_index, model_index) >= len(row):
                continue
            shutdown_date = _iso_date(row[shutdown_index].text)
            if not shutdown_date:
                continue
            model_cell = row[model_index]
            model_names = model_cell.codes or [model_cell.text]
            replacement = ""
            if replacement_index is not None and replacement_index < len(row):
                replacement_cell = row[replacement_index]
                replacement = (
                    replacement_cell.codes[0]
                    if replacement_cell.codes
                    else replacement_cell.text
                )
            for model_name in model_names:
                normalized = normalize_model_name(model_name)
                if not lifecycle_check_model(provider, normalized):
                    continue
                observed.append(
                    {
                        "model": normalized,
                        "shutdown_date": shutdown_date,
                        "replacement": replacement.strip(),
                    }
                )
    return observed


def compare_catalog(
    provider: str,
    catalog: dict[str, dict[str, str]],
    observed: list[dict[str, str]],
) -> list[str]:
    observed_by_model: dict[str, list[dict[str, str]]] = {}
    for entry in observed:
        observed_by_model.setdefault(entry["model"], []).append(entry)

    errors = []
    for model, entries in sorted(observed_by_model.items()):
        expected = catalog.get(model)
        if expected is None:
            errors.append(
                f"{provider}: official source added lifecycle model {model}; "
                "update utils/ai_model_catalog.py"
            )
            continue
        matching_dates = [
            entry
            for entry in entries
            if entry["shutdown_date"] == expected["shutdown_date"]
        ]
        if not matching_dates:
            dates = ", ".join(sorted({entry["shutdown_date"] for entry in entries}))
            errors.append(
                f"{provider}: {model} shutdown is {dates}, catalog has "
                f"{expected['shutdown_date']}"
            )
            continue
        expected_replacement = normalize_model_name(expected.get("replacement", ""))
        if expected_replacement and not any(
            expected_replacement in normalize_model_name(entry.get("replacement", ""))
            for entry in matching_dates
        ):
            replacements = ", ".join(
                sorted({entry.get("replacement", "") for entry in matching_dates})
            )
            errors.append(
                f"{provider}: {model} replacement is {replacements or 'blank'}, "
                f"catalog has {expected['replacement']}"
            )

    for model in sorted(catalog):
        if not lifecycle_check_model(provider, model):
            continue
        if model not in observed_by_model:
            errors.append(
                f"{provider}: catalog model {model} is absent from the official "
                "lifecycle tables"
            )
    return errors


def _fetch(url: str, timeout: int) -> str:
    parsed = urlsplit(url)
    allowed_hosts = {
        "ai.google.dev",
        "developers.openai.com",
        "platform.claude.com",
    }
    if parsed.scheme != "https" or parsed.hostname not in allowed_hosts:
        raise ValueError(f"refusing non-official lifecycle URL: {url}")
    request = Request(  # noqa: S310 - URL is restricted to official HTTPS hosts.
        url,
        headers={"User-Agent": "DUMB AI model lifecycle checker/1.0"},
    )
    with urlopen(  # noqa: S310 - URL is restricted to official HTTPS hosts.
        request, timeout=timeout
    ) as response:
        return response.read().decode("utf-8", errors="replace")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args()

    providers = (
        ("google_gemini", GEMINI_DEPRECATIONS_URL, GEMINI_MODEL_LIFECYCLE),
        ("openai", OPENAI_DEPRECATIONS_URL, OPENAI_MODEL_LIFECYCLE),
        ("anthropic", ANTHROPIC_DEPRECATIONS_URL, ANTHROPIC_MODEL_LIFECYCLE),
    )
    errors = []
    for provider, url, catalog in providers:
        try:
            observed = parse_lifecycle_rows(provider, _fetch(url, args.timeout))
        except Exception as exc:
            errors.append(f"{provider}: could not inspect {url}: {exc}")
            continue
        errors.extend(compare_catalog(provider, catalog, observed))
        print(
            f"{provider}: checked {len(observed)} official lifecycle model rows "
            f"against {len(catalog)} catalog entries"
        )
    if errors:
        print("\nLifecycle catalog check failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print("AI model lifecycle catalog matches the official provider tables.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
