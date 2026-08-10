"""Provider model lifecycle and DUMB AI Assist compatibility metadata."""

from __future__ import annotations

from datetime import datetime, timezone
import re

GEMINI_DEPRECATIONS_URL = "https://ai.google.dev/gemini-api/docs/deprecations"
OPENAI_DEPRECATIONS_URL = "https://developers.openai.com/api/docs/deprecations"
ANTHROPIC_DEPRECATIONS_URL = (
    "https://platform.claude.com/docs/en/about-claude/model-deprecations"
)


def _catalog(entries: list[tuple[str, str, str]]) -> dict[str, dict[str, str]]:
    return {
        model: {"shutdown_date": shutdown_date, "replacement": replacement}
        for model, shutdown_date, replacement in entries
    }


GEMINI_MODEL_LIFECYCLE = _catalog(
    [
        ("gemini-3.1-flash-lite", "2027-05-07", "gemini-3.5-flash-lite"),
        (
            "gemini-3.1-flash-image-preview",
            "2026-06-25",
            "gemini-3.1-flash-image",
        ),
        ("gemini-3-pro-image-preview", "2026-06-25", "gemini-3-pro-image"),
        ("gemini-3-pro-preview", "2026-03-09", "gemini-3.1-pro-preview"),
        (
            "gemini-3.1-flash-lite-preview",
            "2026-05-25",
            "gemini-3.1-flash-lite",
        ),
        (
            "gemini-2.5-pro-preview-03-25",
            "2025-12-02",
            "gemini-3.1-pro-preview",
        ),
        (
            "gemini-2.5-pro-preview-05-06",
            "2025-12-02",
            "gemini-3.1-pro-preview",
        ),
        (
            "gemini-2.5-pro-preview-06-05",
            "2025-12-02",
            "gemini-3.1-pro-preview",
        ),
        (
            "gemini-2.5-flash-image",
            "2026-10-02",
            "gemini-3.1-flash-image-preview",
        ),
        (
            "gemini-2.5-flash-lite-preview-09-2025",
            "2026-03-31",
            "gemini-3.1-flash-lite",
        ),
        ("gemini-2.5-flash-preview-05-20", "2025-11-18", "gemini-3.6-flash"),
        (
            "gemini-2.5-flash-image-preview",
            "2026-01-15",
            "gemini-2.5-flash-image",
        ),
        ("gemini-2.5-flash-preview-09-25", "2026-02-17", "gemini-3.6-flash"),
        ("gemini-2.0-flash", "2026-06-01", "gemini-3.6-flash"),
        ("gemini-2.0-flash-001", "2026-06-01", "gemini-3.6-flash"),
        ("gemini-2.0-flash-lite", "2026-06-01", "gemini-3.1-flash-lite"),
        ("gemini-2.0-flash-lite-001", "2026-06-01", "gemini-3.1-flash-lite"),
        (
            "gemini-2.0-flash-preview-image-generation",
            "2025-11-14",
            "gemini-2.5-flash-image",
        ),
        (
            "gemini-2.0-flash-lite-preview",
            "2025-12-09",
            "gemini-2.5-flash-lite",
        ),
        (
            "gemini-2.0-flash-lite-preview-02-05",
            "2025-12-09",
            "gemini-2.5-flash-lite",
        ),
        (
            "gemini-robotics-er-1.5-preview",
            "2026-04-30",
            "gemini-robotics-er-1.6-preview",
        ),
    ]
)


OPENAI_MODEL_LIFECYCLE = _catalog(
    [
        ("gpt-5-2025-08-07", "2026-12-11", "gpt-5.6-sol"),
        ("gpt-5-mini-2025-08-07", "2026-12-11", "gpt-5.6-terra"),
        ("gpt-5-nano-2025-08-07", "2026-12-11", "gpt-5.6-luna"),
        ("gpt-5-pro-2025-10-06", "2026-12-11", "gpt-5.6-sol"),
        ("o3-2025-04-16", "2026-12-11", "gpt-5.6-sol"),
        ("o3-pro-2025-06-10", "2026-12-11", "gpt-5.6-sol"),
        ("gpt-5.2-chat-latest", "2026-08-10", "gpt-5.6-sol"),
        ("gpt-5.3-chat-latest", "2026-08-10", "gpt-5.6-sol"),
        ("gpt-4o-mini-search-preview-2025-03-11", "2026-07-23", "gpt-5.6-terra"),
        ("gpt-4o-search-preview-2025-03-11", "2026-07-23", "gpt-5.6-terra"),
        ("gpt-5-chat-latest", "2026-07-23", "gpt-5.6-sol"),
        ("gpt-5-codex", "2026-07-23", "gpt-5.6-sol"),
        ("gpt-5.1-chat-latest", "2026-07-23", "gpt-5.6-sol"),
        ("gpt-5.1-codex", "2026-07-23", "gpt-5.6-sol"),
        ("gpt-5.1-codex-max", "2026-07-23", "gpt-5.6-sol"),
        ("gpt-5.1-codex-mini", "2026-07-23", "gpt-5.6-terra"),
        ("gpt-5.2-codex", "2026-07-23", "gpt-5.6-sol"),
        ("gpt-3.5-turbo-0125", "2026-10-23", "gpt-5.6-terra"),
        ("gpt-3.5-turbo", "2026-10-23", "gpt-5.6-terra"),
        ("gpt-3.5-turbo-completions", "2026-10-23", "gpt-5.6-terra"),
        ("gpt-4-0613", "2026-10-23", "gpt-5.6-sol"),
        ("gpt-4", "2026-10-23", "gpt-5.6-sol"),
        ("gpt-4-0613-completions", "2026-10-23", "gpt-5.6-sol"),
        ("gpt-4-completions", "2026-10-23", "gpt-5.6-sol"),
        ("gpt-4-1106-preview", "2026-10-23", "gpt-5.6-sol"),
        ("gpt-4-turbo", "2026-10-23", "gpt-5.6-sol"),
        ("gpt-4-turbo-2024-04-09", "2026-10-23", "gpt-5.6-sol"),
        ("gpt-4-turbo-completions", "2026-10-23", "gpt-5.6-sol"),
        ("gpt-4.1-nano", "2026-10-23", "gpt-5.6-luna"),
        ("gpt-4.1-nano-2025-04-14", "2026-10-23", "gpt-5.6-luna"),
        ("gpt-4o-2024-05-13", "2026-10-23", "gpt-5.6-sol"),
        ("o1-2024-12-17", "2026-10-23", "gpt-5.6-sol"),
        ("o1", "2026-10-23", "gpt-5.6-sol"),
        ("o1-pro-2025-03-19", "2026-10-23", "gpt-5.6-sol"),
        ("o1-pro", "2026-10-23", "gpt-5.6-sol"),
        ("o3-mini-2025-01-31", "2026-10-23", "gpt-5.6-sol"),
        ("o3-mini", "2026-10-23", "gpt-5.6-sol"),
        ("o4-mini-2025-04-16", "2026-10-23", "gpt-5.6-terra"),
        ("o4-mini", "2026-10-23", "gpt-5.6-terra"),
        ("gpt-4-0314", "2026-03-26", "gpt-5"),
        ("gpt-4-0125-preview", "2026-03-26", "gpt-5"),
        ("gpt-4-turbo-preview", "2026-03-26", "gpt-5"),
        ("gpt-4-turbo-preview-completions", "2026-03-26", "gpt-5"),
        ("gpt-3.5-turbo-instruct", "2026-09-28", "gpt-5.6-terra"),
        ("babbage-002", "2026-09-28", "gpt-5.6-terra"),
        ("davinci-002", "2026-09-28", "gpt-5.6-terra"),
        ("gpt-3.5-turbo-1106", "2026-09-28", "gpt-5.6-terra"),
    ]
)

ANTHROPIC_MODEL_LIFECYCLE = _catalog(
    [
        ("claude-opus-4-1-20250805", "2026-08-05", "claude-opus-4-8"),
        ("claude-sonnet-4-20250514", "2026-06-15", "claude-sonnet-4-6"),
        ("claude-opus-4-20250514", "2026-06-15", "claude-opus-4-8"),
        (
            "claude-3-haiku-20240307",
            "2026-04-20",
            "claude-haiku-4-5-20251001",
        ),
        (
            "claude-3-5-haiku-20241022",
            "2026-02-19",
            "claude-haiku-4-5-20251001",
        ),
        ("claude-3-7-sonnet-20250219", "2026-02-19", "claude-sonnet-4-6"),
        ("claude-3-5-sonnet-20240620", "2025-10-28", "claude-sonnet-4-6"),
        ("claude-3-5-sonnet-20241022", "2025-10-28", "claude-sonnet-4-6"),
        ("claude-3-opus-20240229", "2026-01-05", "claude-opus-4-8"),
        ("claude-2.0", "2025-07-21", "claude-opus-4-8"),
        ("claude-2.1", "2025-07-21", "claude-opus-4-8"),
        ("claude-3-sonnet-20240229", "2025-07-21", "claude-sonnet-4-6"),
        ("claude-1.0", "2024-11-06", "claude-haiku-4-5-20251001"),
        ("claude-1.1", "2024-11-06", "claude-haiku-4-5-20251001"),
        ("claude-1.2", "2024-11-06", "claude-haiku-4-5-20251001"),
        ("claude-1.3", "2024-11-06", "claude-haiku-4-5-20251001"),
        ("claude-instant-1.0", "2024-11-06", "claude-haiku-4-5-20251001"),
        ("claude-instant-1.1", "2024-11-06", "claude-haiku-4-5-20251001"),
        ("claude-instant-1.2", "2024-11-06", "claude-haiku-4-5-20251001"),
    ]
)


_OPENAI_UNSUPPORTED_PATTERNS = (
    (
        re.compile(r"(?:^|[-_.])(embedding|moderation)(?:$|[-_.])"),
        "embedding or moderation",
    ),
    (
        re.compile(r"(?:^|[-_.])(audio|realtime|transcribe|tts|whisper)(?:$|[-_.])"),
        "audio or realtime",
    ),
    (
        re.compile(r"(?:^|[-_.])(image|dall-e|sora|video)(?:$|[-_.])"),
        "image or video generation",
    ),
    (
        re.compile(r"(computer-use|deep-research|search-preview)"),
        "specialized tool",
    ),
)
_GEMINI_UNSUPPORTED_PATTERN = re.compile(
    r"(embedding|imagen|veo|lyria|robotics|image|live|audio|tts|computer-use)"
)


def normalize_model_name(model: str) -> str:
    return str(model or "").strip().removeprefix("models/").strip().lower()


def model_lifecycle(
    provider: str, model: str, as_of: str | None = None
) -> dict[str, str] | None:
    normalized_provider = str(provider or "").strip().lower()
    normalized_model = normalize_model_name(model)
    if normalized_provider in {"gemini", "google_gemini"}:
        lifecycle = GEMINI_MODEL_LIFECYCLE.get(normalized_model)
        source_url = GEMINI_DEPRECATIONS_URL
    elif normalized_provider == "openai":
        lifecycle = OPENAI_MODEL_LIFECYCLE.get(normalized_model)
        source_url = OPENAI_DEPRECATIONS_URL
    elif normalized_provider in {"anthropic", "claude"}:
        lifecycle = ANTHROPIC_MODEL_LIFECYCLE.get(normalized_model)
        source_url = ANTHROPIC_DEPRECATIONS_URL
    else:
        return None
    if not lifecycle:
        return None
    current_date = as_of or datetime.now(timezone.utc).date().isoformat()
    shutdown_date = lifecycle["shutdown_date"]
    return {
        "model": normalized_model,
        "status": "retired" if current_date >= shutdown_date else "deprecated",
        "shutdown_date": shutdown_date,
        "replacement": lifecycle["replacement"],
        "source_url": source_url,
        "provider": normalized_provider,
    }


def model_compatibility(provider: str, model: str) -> dict[str, str] | None:
    normalized_provider = str(provider or "").strip().lower()
    normalized_model = normalize_model_name(model)
    if not normalized_model:
        return None
    if normalized_provider == "openai":
        for pattern, category in _OPENAI_UNSUPPORTED_PATTERNS:
            if pattern.search(normalized_model):
                return {
                    "model": normalized_model,
                    "status": "unsupported",
                    "api_surface": "responses",
                    "reason": (
                        f"This is an {category} model. DUMB AI Assist requires a "
                        "model that returns diagnostic text."
                    ),
                }
        return {
            "model": normalized_model,
            "status": "supported",
            "api_surface": "responses",
            "reason": "Native OpenAI text requests use the Responses API.",
        }
    if normalized_provider in {"gemini", "google_gemini"}:
        if _GEMINI_UNSUPPORTED_PATTERN.search(normalized_model):
            return {
                "model": normalized_model,
                "status": "unsupported",
                "api_surface": "generateContent",
                "reason": (
                    "This is a specialized or non-text Gemini model. DUMB AI "
                    "Assist requires a model that returns diagnostic text."
                ),
            }
        return {
            "model": normalized_model,
            "status": "supported",
            "api_surface": "generateContent",
            "reason": "Gemini text requests use generateContent.",
        }
    if normalized_provider in {"anthropic", "claude"}:
        return {
            "model": normalized_model,
            "status": "supported",
            "api_surface": "messages",
            "reason": "Native Anthropic text requests use the Messages API.",
        }
    return None


def lifecycle_check_model(provider: str, model: str) -> bool:
    normalized_provider = str(provider or "").strip().lower()
    normalized_model = normalize_model_name(model)
    if normalized_provider == "openai":
        if normalized_model.startswith("ft-") or not normalized_model.startswith(
            ("gpt-", "o1", "o3", "o4", "babbage-", "davinci-")
        ):
            return False
        compatibility = model_compatibility(provider, normalized_model)
        return compatibility is not None and compatibility["status"] == "supported"
    if normalized_provider in {"gemini", "google_gemini"}:
        compatibility = model_compatibility(provider, normalized_model)
        return compatibility is not None and compatibility["status"] == "supported"
    if normalized_provider in {"anthropic", "claude"}:
        return normalized_model.startswith("claude-")
    return False
