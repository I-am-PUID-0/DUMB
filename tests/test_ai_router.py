import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from api.routers import ai


class AiRouterTests(unittest.TestCase):
    def test_active_profile_is_authoritative_over_stale_top_level_fields(self):
        profile = {
            "id": "litellm-profile",
            "name": "LiteLLM",
            "provider": "litellm",
            "base_url": "https://gateway.example.invalid/v1",
            "model": "stack-model",
            "api_key": "profile-secret",
            "timeout_sec": 45,
            "temperature": 0.3,
        }
        config = {
            "dumb": {
                "ai": {
                    "provider": "gemini",
                    "base_url": ai.GEMINI_API_BASE_URL,
                    "model": "gemini-stale",
                    "api_key": "stale-secret",
                    "active_profile_id": profile["id"],
                    "profiles": [profile],
                }
            }
        }

        with patch.object(ai.CONFIG_MANAGER, "config", config):
            effective = ai._ai_config()

        self.assertEqual(effective["provider"], "litellm")
        self.assertEqual(effective["base_url"], "https://gateway.example.invalid/v1")
        self.assertEqual(effective["model"], "stack-model")
        self.assertEqual(effective["api_key"], "profile-secret")
        self.assertEqual(effective["active_profile_id"], profile["id"])

    def test_missing_active_profile_is_detached_on_load(self):
        config = {
            "dumb": {
                "ai": {
                    "active_profile_id": "missing-profile",
                    "profiles": [],
                }
            }
        }

        with patch.object(ai.CONFIG_MANAGER, "config", config):
            effective = ai._ai_config()

        self.assertEqual(effective["active_profile_id"], "")

    def test_redact_value_masks_nested_secrets(self):
        payload = {
            "api_key": "abc123",
            "nested": {"password": "secret", "safe": "hello token=raw"},
            "items": [{"client_secret": "hidden"}],
        }

        redacted = ai._redact_value(payload)

        self.assertEqual(redacted["api_key"], "[REDACTED]")
        self.assertEqual(redacted["nested"]["password"], "[REDACTED]")
        self.assertIn("token=[REDACTED]", redacted["nested"]["safe"])
        self.assertEqual(redacted["items"][0]["client_secret"], "[REDACTED]")

    def test_public_settings_do_not_return_api_key(self):
        public = ai._public_settings(
            {
                "api_key": "sk-test",
                "enabled": True,
                "provider": "gemini",
                "model": "gemini-2.0-flash-lite",
                "profiles": [
                    {
                        "id": "gemini-profile",
                        "name": "Gemini",
                        "provider": "gemini",
                        "model": "gemini-2.0-flash-lite",
                        "api_key": "gemini-secret",
                    },
                    {
                        "id": "ollama-profile",
                        "name": "Ollama",
                        "api_key": "",
                    },
                ],
            }
        )

        self.assertNotIn("api_key", public)
        self.assertTrue(public["api_key_configured"])
        self.assertTrue(public["enabled"])
        self.assertNotIn("api_key", public["profiles"][0])
        self.assertTrue(public["profiles"][0]["api_key_configured"])
        self.assertFalse(public["profiles"][1]["api_key_configured"])
        self.assertEqual(public["model_lifecycle"]["status"], "retired")
        self.assertEqual(
            public["model_lifecycle"]["replacement"], "gemini-3.1-flash-lite"
        )
        self.assertEqual(public["profiles"][0]["model_lifecycle"]["status"], "retired")
        self.assertIsNone(public["profiles"][1]["model_lifecycle"])
        self.assertEqual(public["model_compatibility"]["status"], "supported")
        self.assertIsNone(public["profiles"][1]["model_compatibility"])

    def test_gemini_model_lifecycle_distinguishes_retired_and_deprecated(self):
        retired = ai._gemini_model_lifecycle(
            "models/gemini-2.0-flash-lite", as_of="2026-07-24"
        )
        deprecated = ai._gemini_model_lifecycle("gemini-2.5-flash", as_of="2026-07-24")

        self.assertEqual(retired["status"], "retired")
        self.assertEqual(retired["shutdown_date"], "2026-06-01")
        self.assertEqual(retired["replacement"], "gemini-3.1-flash-lite")
        self.assertEqual(deprecated["status"], "deprecated")
        self.assertEqual(deprecated["shutdown_date"], "2026-10-16")
        self.assertEqual(deprecated["replacement"], "gemini-3.6-flash")
        self.assertIsNone(
            ai._gemini_model_lifecycle("gemini-3.5-flash-lite", as_of="2026-07-24")
        )

    def test_save_provider_profile_creates_active_profile_and_hides_key(self):
        config = {"dumb": {"ai": {}}}
        request = ai.AiProviderProfileRequest(
            name="Free Gemini",
            provider="gemini",
            base_url="https://gateway.example.invalid/gemini",
            model="gemini-3.5-flash-lite",
            api_key="gemini-secret",
            timeout_sec=45,
            temperature=0.4,
        )

        with (
            patch.object(ai.CONFIG_MANAGER, "config", config),
            patch.object(ai.CONFIG_MANAGER, "save_config") as save_config,
            patch.object(ai, "record_ai_config_change"),
        ):
            result = ai.save_ai_provider_profile(request, current_user="tester")

        stored = config["dumb"]["ai"]
        self.assertEqual(len(stored["profiles"]), 1)
        self.assertEqual(stored["active_profile_id"], stored["profiles"][0]["id"])
        self.assertEqual(stored["provider"], "gemini")
        self.assertEqual(stored["base_url"], ai.GEMINI_API_BASE_URL)
        self.assertEqual(stored["api_key"], "gemini-secret")
        self.assertEqual(stored["profiles"][0]["api_key"], "gemini-secret")
        self.assertEqual(stored["profiles"][0]["base_url"], ai.GEMINI_API_BASE_URL)
        self.assertTrue(result["api_key_configured"])
        self.assertTrue(result["profiles"][0]["api_key_configured"])
        self.assertNotIn("api_key", result)
        self.assertNotIn("api_key", result["profiles"][0])
        save_config.assert_called_once_with()

    def test_save_provider_profile_preserves_stored_key_when_blank(self):
        profile = {
            "id": "profile-1",
            "name": "Gemini",
            "provider": "gemini",
            "base_url": ai.GEMINI_API_BASE_URL,
            "model": "gemini-3.5-flash-lite",
            "api_key": "stored-secret",
            "timeout_sec": 60,
            "temperature": 0.2,
        }
        config = {
            "dumb": {
                "ai": {
                    **{
                        field: profile[field] for field in ai.AI_PROVIDER_PROFILE_FIELDS
                    },
                    "active_profile_id": "profile-1",
                    "profiles": [profile],
                }
            }
        }
        request = ai.AiProviderProfileRequest(
            id="profile-1",
            name="Gemini Fast",
            provider="gemini",
            base_url=ai.GEMINI_API_BASE_URL,
            model="gemini-3.6-flash",
            api_key="",
        )

        with (
            patch.object(ai.CONFIG_MANAGER, "config", config),
            patch.object(ai.CONFIG_MANAGER, "save_config"),
            patch.object(ai, "record_ai_config_change"),
        ):
            ai.save_ai_provider_profile(request, current_user="tester")

        stored = config["dumb"]["ai"]
        self.assertEqual(stored["profiles"][0]["name"], "Gemini Fast")
        self.assertEqual(stored["profiles"][0]["model"], "gemini-3.6-flash")
        self.assertEqual(stored["profiles"][0]["api_key"], "stored-secret")
        self.assertEqual(stored["api_key"], "stored-secret")

    def test_activate_and_delete_provider_profiles(self):
        gemini = {
            "id": "gemini-profile",
            "name": "Gemini",
            "provider": "gemini",
            "base_url": ai.GEMINI_API_BASE_URL,
            "model": "gemini-3.5-flash-lite",
            "api_key": "gemini-secret",
            "timeout_sec": 60,
            "temperature": 0.2,
        }
        ollama = {
            "id": "ollama-profile",
            "name": "Local",
            "provider": "ollama",
            "base_url": "http://ollama:11434",
            "model": "llama3.1",
            "api_key": "",
            "timeout_sec": 90,
            "temperature": 0.1,
        }
        config = {
            "dumb": {
                "ai": {
                    **{field: gemini[field] for field in ai.AI_PROVIDER_PROFILE_FIELDS},
                    "active_profile_id": gemini["id"],
                    "profiles": [gemini, ollama],
                }
            }
        }

        with (
            patch.object(ai.CONFIG_MANAGER, "config", config),
            patch.object(ai.CONFIG_MANAGER, "save_config"),
            patch.object(ai, "record_ai_config_change"),
        ):
            activated = ai.activate_ai_provider_profile(
                ollama["id"], current_user="tester"
            )
            deleted = ai.delete_ai_provider_profile(ollama["id"], current_user="tester")

        self.assertEqual(activated["active_profile_id"], ollama["id"])
        self.assertEqual(activated["provider"], "ollama")
        self.assertFalse(activated["api_key_configured"])
        self.assertEqual(deleted["active_profile_id"], gemini["id"])
        self.assertEqual(deleted["provider"], "gemini")
        self.assertTrue(deleted["api_key_configured"])
        self.assertEqual(
            [profile["id"] for profile in deleted["profiles"]],
            [gemini["id"]],
        )

    def test_update_ai_settings_synchronizes_active_profile(self):
        profile = {
            "id": "profile-1",
            "name": "Gemini",
            "provider": "gemini",
            "base_url": ai.GEMINI_API_BASE_URL,
            "model": "gemini-3.5-flash-lite",
            "api_key": "stored-secret",
            "timeout_sec": 60,
            "temperature": 0.2,
        }
        config = {
            "dumb": {
                "ai": {
                    **{
                        field: profile[field] for field in ai.AI_PROVIDER_PROFILE_FIELDS
                    },
                    "active_profile_id": profile["id"],
                    "profiles": [profile],
                }
            }
        }

        with (
            patch.object(ai.CONFIG_MANAGER, "config", config),
            patch.object(ai.CONFIG_MANAGER, "save_config"),
            patch.object(ai, "record_ai_config_change"),
        ):
            result = ai.update_ai_settings(
                ai.AiSettingsUpdate(model="gemini-3.6-flash"),
                current_user="tester",
            )

        self.assertEqual(result["model"], "gemini-3.6-flash")
        self.assertEqual(
            config["dumb"]["ai"]["profiles"][0]["model"],
            "gemini-3.6-flash",
        )
        self.assertEqual(
            config["dumb"]["ai"]["profiles"][0]["api_key"],
            "stored-secret",
        )

    def test_update_ai_settings_can_detach_without_overwriting_profile(self):
        profile = {
            "id": "profile-1",
            "name": "Saved Gemini",
            "provider": "gemini",
            "base_url": ai.GEMINI_API_BASE_URL,
            "model": "gemini-saved",
            "api_key": "stored-secret",
            "timeout_sec": 60,
            "temperature": 0.2,
        }
        config = {
            "dumb": {
                "ai": {
                    **ai.DEFAULT_AI_CONFIG,
                    **{
                        field: profile[field] for field in ai.AI_PROVIDER_PROFILE_FIELDS
                    },
                    "active_profile_id": profile["id"],
                    "profiles": [profile],
                }
            }
        }

        with (
            patch.object(ai.CONFIG_MANAGER, "config", config),
            patch.object(ai.CONFIG_MANAGER, "save_config"),
            patch.object(ai, "record_ai_config_change"),
        ):
            result = ai.update_ai_settings(
                ai.AiSettingsUpdate(
                    active_profile_id="",
                    provider="ollama",
                    base_url="http://ollama:11434",
                    model="llama3.1",
                ),
                current_user="tester",
            )

        self.assertEqual(result["active_profile_id"], "")
        self.assertEqual(config["dumb"]["ai"]["provider"], "ollama")
        self.assertEqual(
            config["dumb"]["ai"]["profiles"][0]["model"],
            "gemini-saved",
        )
        self.assertEqual(
            config["dumb"]["ai"]["profiles"][0]["api_key"],
            "stored-secret",
        )

    def test_effective_ai_config_preserves_stored_api_key_when_blank(self):
        config = {
            "dumb": {
                "ai": {
                    "provider": "openai",
                    "base_url": "https://api.openai.com/v1",
                    "model": "gpt-4.1-mini",
                    "api_key": "stored-key",
                }
            }
        }
        request = ai.AiProviderRequest(
            provider="openai",
            base_url="https://gateway.example.invalid/v1",
            model="gpt-4.1-mini",
            api_key="",
        )

        with patch.object(ai.CONFIG_MANAGER, "config", config):
            effective = ai._effective_ai_config(request)

        self.assertEqual(effective["api_key"], "stored-key")
        self.assertEqual(effective["base_url"], ai.OPENAI_API_BASE_URL)

    def test_native_hosted_providers_ignore_custom_endpoints(self):
        messages = [
            {"role": "system", "content": "Be concise."},
            {"role": "user", "content": "Test"},
        ]

        with patch.object(
            ai,
            "_post_json",
            return_value={"content": [{"type": "text", "text": "Claude response"}]},
        ) as anthropic_post:
            result = ai._call_ai_messages_result(
                {
                    "provider": "anthropic",
                    "base_url": "https://gateway.example.invalid/anthropic",
                    "model": "claude-test",
                    "api_key": "anthropic-key",
                },
                messages,
            )

        self.assertEqual(result["content"], "Claude response")
        self.assertEqual(anthropic_post.call_args.args[0], ai.ANTHROPIC_MESSAGES_URL)
        self.assertNotIn("temperature", anthropic_post.call_args.args[2])

        with patch.object(
            ai,
            "_post_json",
            return_value={
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "OpenAI response"}],
                    }
                ],
                "usage": {"input_tokens": 2, "output_tokens": 1},
            },
        ) as openai_post:
            result = ai._call_ai_messages_result(
                {
                    "provider": "openai",
                    "base_url": "https://gateway.example.invalid/v1",
                    "model": "gpt-test",
                    "api_key": "openai-key",
                },
                messages,
            )

        self.assertEqual(result["content"], "OpenAI response")
        self.assertEqual(
            openai_post.call_args.args[0],
            f"{ai.OPENAI_API_BASE_URL}/responses",
        )
        payload = openai_post.call_args.args[2]
        self.assertEqual(payload["model"], "gpt-test")
        self.assertEqual(payload["input"][0]["role"], "developer")
        self.assertEqual(payload["input"][1]["role"], "user")
        self.assertEqual(payload["max_output_tokens"], 1600)
        self.assertFalse(payload["store"])
        self.assertNotIn("temperature", payload)
        self.assertEqual(result["usage"]["total_tokens"], 3)

    def test_native_openai_rejects_retired_and_non_text_models(self):
        messages = [{"role": "user", "content": "Test"}]
        for model, detail in (
            ("gpt-5-codex", "retired model gpt-5-codex"),
            ("text-embedding-3-small", "embedding or moderation model"),
        ):
            with (
                patch.object(ai, "_post_json") as post_json,
                self.assertRaisesRegex(ai.HTTPException, detail),
            ):
                ai._call_ai_messages_result(
                    {
                        "provider": "openai",
                        "model": model,
                        "api_key": "openai-key",
                    },
                    messages,
                )
            post_json.assert_not_called()

    def test_native_openai_returns_clear_error_when_response_has_no_text(self):
        with (
            patch.object(
                ai,
                "_post_json",
                return_value={
                    "status": "incomplete",
                    "incomplete_details": {"reason": "max_output_tokens"},
                    "output": [],
                },
            ),
            self.assertRaisesRegex(
                ai.HTTPException,
                "OpenAI returned no response text \\(max_output_tokens\\)",
            ),
        ):
            ai._call_ai_messages_result(
                {
                    "provider": "openai",
                    "model": "gpt-5.3-codex",
                    "api_key": "openai-key",
                },
                [{"role": "user", "content": "Test"}],
            )

    def test_list_provider_models_reads_ollama_tags(self):
        ai_config = {
            "provider": "ollama",
            "base_url": "http://ollama:11434",
            "timeout_sec": 12,
        }

        with patch.object(
            ai,
            "_get_json",
            return_value={
                "models": [
                    {
                        "name": "llama3.1:latest",
                        "size": 123,
                        "modified_at": "2026-06-18T00:00:00Z",
                    },
                    {"model": "qwen2.5:7b"},
                ]
            },
        ) as get_json:
            result = ai._list_provider_models(ai_config)

        get_json.assert_called_once_with(
            "http://ollama:11434/api/tags",
            {"content-type": "application/json"},
            12,
        )
        self.assertEqual(result["provider"], "ollama")
        self.assertEqual(
            [model["name"] for model in result["models"]],
            ["llama3.1:latest", "qwen2.5:7b"],
        )

    def test_native_openai_model_discovery_adds_lifecycle_and_compatibility(self):
        ai_config = {
            "provider": "openai",
            "api_key": "openai-key",
            "timeout_sec": 15,
        }
        with patch.object(
            ai,
            "_get_json",
            return_value={
                "data": [
                    {"id": "text-embedding-3-small", "owned_by": "openai"},
                    {"id": "gpt-5.3-codex", "owned_by": "openai"},
                    {"id": "gpt-5-codex", "owned_by": "openai"},
                ]
            },
        ) as get_json:
            result = ai._list_provider_models(ai_config)

        self.assertEqual(
            [entry["name"] for entry in result["models"]],
            ["gpt-5-codex", "gpt-5.3-codex", "text-embedding-3-small"],
        )
        retired, current, embedding = result["models"]
        self.assertEqual(retired["lifecycle"]["status"], "retired")
        self.assertEqual(retired["compatibility"]["api_surface"], "responses")
        self.assertNotIn("lifecycle", current)
        self.assertEqual(current["compatibility"]["status"], "supported")
        self.assertEqual(embedding["compatibility"]["status"], "unsupported")
        get_json.assert_called_once_with(
            f"{ai.OPENAI_API_BASE_URL}/models",
            {
                "content-type": "application/json",
                "authorization": "Bearer openai-key",
            },
            15,
        )

    def test_anthropic_model_discovery_adds_lifecycle_metadata(self):
        with patch.object(
            ai,
            "_get_json",
            return_value={
                "data": [
                    {
                        "id": "claude-opus-4-1-20250805",
                        "display_name": "Claude Opus 4.1",
                        "created_at": "2025-08-05T00:00:00Z",
                    },
                    {
                        "id": "claude-sonnet-4-6",
                        "display_name": "Claude Sonnet 4.6",
                    },
                ]
            },
        ) as get_json:
            result = ai._list_provider_models(
                {
                    "provider": "anthropic",
                    "api_key": "anthropic-key",
                    "timeout_sec": 12,
                }
            )

        retired, current = result["models"]
        self.assertEqual(retired["lifecycle"]["status"], "deprecated")
        self.assertEqual(retired["lifecycle"]["replacement"], "claude-opus-4-8")
        self.assertEqual(current["compatibility"]["api_surface"], "messages")
        get_json.assert_called_once_with(
            "https://api.anthropic.com/v1/models?limit=1000",
            {
                "x-api-key": "anthropic-key",
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            12,
        )

    def test_open_webui_uses_api_routes_for_chat_and_models(self):
        ai_config = {
            "provider": "open_webui",
            "base_url": "http://open-webui:3000",
            "api_key": "owui-key",
            "model": "llama3.1",
            "timeout_sec": 15,
            "temperature": 0.1,
        }
        messages = [{"role": "user", "content": "test"}]

        with patch.object(
            ai,
            "_post_json",
            return_value={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 3,
                    "total_tokens": 13,
                },
            },
        ) as post_json:
            response = ai._call_ai_messages(ai_config, messages)

        self.assertEqual(response, "ok")
        self.assertEqual(
            post_json.call_args.args[0],
            "http://open-webui:3000/api/chat/completions",
        )

        with patch.object(
            ai,
            "_get_json",
            return_value={
                "data": [
                    {"id": "llama3.1", "owned_by": "ollama"},
                    {"id": "gpt-4o-mini", "owned_by": "openai"},
                ]
            },
        ) as get_json:
            models = ai._list_provider_models(ai_config)

        self.assertEqual(models["models"][0]["name"], "llama3.1")
        self.assertEqual(models["models"][0]["source"], "local")
        self.assertEqual(models["models"][1]["source"], "external")
        self.assertEqual(
            get_json.call_args.args[0], "http://open-webui:3000/api/models"
        )

    def test_call_ai_messages_result_returns_usage(self):
        ai_config = {
            "provider": "open_webui",
            "base_url": "http://open-webui:3000",
            "api_key": "owui-key",
            "model": "llama3.1",
        }

        with patch.object(
            ai,
            "_post_json",
            return_value={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 2},
            },
        ):
            result = ai._call_ai_messages_result(
                ai_config, [{"role": "user", "content": "test"}]
            )

        self.assertEqual(result["content"], "ok")
        self.assertEqual(result["usage"]["prompt_tokens"], 7)
        self.assertEqual(result["usage"]["completion_tokens"], 2)
        self.assertEqual(result["usage"]["total_tokens"], 9)

    def test_litellm_uses_openai_compatible_chat_and_models(self):
        ai_config = {
            "provider": "litellm",
            "base_url": "http://litellm:4000/v1",
            "api_key": "proxy-key",
            "model": "Local - Qwen 2.5 14B",
        }

        with patch.object(
            ai,
            "_post_json",
            return_value={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2},
            },
        ) as post_json:
            result = ai._call_ai_messages_result(
                ai_config, [{"role": "user", "content": "test"}]
            )

        self.assertEqual(result["content"], "ok")
        self.assertEqual(
            post_json.call_args.args[0], "http://litellm:4000/v1/chat/completions"
        )

        with patch.object(
            ai,
            "_get_json",
            return_value={
                "data": [
                    {"id": "Local - Qwen 2.5 14B", "owned_by": "openai"},
                    {"id": "gpt-4o", "owned_by": "openai"},
                ]
            },
        ) as get_json:
            models = ai._list_provider_models(ai_config)

        self.assertEqual(get_json.call_args.args[0], "http://litellm:4000/v1/models")
        self.assertEqual(models["models"][0]["source"], "local")
        self.assertEqual(models["models"][0]["source_detail"], "local")
        self.assertEqual(models["models"][1]["source"], "external")

    def test_gemini_uses_generate_content_and_normalizes_usage(self):
        ai_config = {
            "provider": "gemini",
            "base_url": "https://gateway.example.invalid/gemini",
            "api_key": "gemini-key",
            "model": "gemini-3.5-flash-lite",
            "timeout_sec": 20,
            "temperature": 0.1,
        }
        messages = [
            {"role": "system", "content": "Use DUMB evidence only."},
            {"role": "user", "content": "What failed?"},
        ]

        with patch.object(
            ai,
            "_post_json",
            return_value={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"text": "Internal reasoning", "thought": True},
                                {"text": "The service failed to start."},
                            ]
                        },
                        "finishReason": "STOP",
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 14,
                    "candidatesTokenCount": 6,
                    "thoughtsTokenCount": 3,
                    "totalTokenCount": 23,
                },
            },
        ) as post_json:
            result = ai._call_ai_messages_result(ai_config, messages, max_tokens=120)

        self.assertEqual(result["content"], "The service failed to start.")
        self.assertEqual(result["usage"]["prompt_tokens"], 14)
        self.assertEqual(result["usage"]["completion_tokens"], 6)
        self.assertEqual(result["usage"]["thoughts_tokens"], 3)
        self.assertEqual(result["usage"]["total_tokens"], 23)
        self.assertEqual(
            post_json.call_args.args[0],
            "https://generativelanguage.googleapis.com/v1beta/models/"
            "gemini-3.5-flash-lite:generateContent",
        )
        self.assertEqual(post_json.call_args.args[1]["x-goog-api-key"], "gemini-key")
        payload = post_json.call_args.args[2]
        self.assertEqual(
            payload["systemInstruction"]["parts"][0]["text"],
            "Use DUMB evidence only.",
        )
        self.assertEqual(payload["contents"][0]["role"], "user")
        self.assertEqual(payload["generationConfig"]["maxOutputTokens"], 120)
        self.assertNotIn("temperature", payload["generationConfig"])

    def test_gemini_model_discovery_filters_non_generation_models(self):
        ai_config = {
            "provider": "google_gemini",
            "base_url": "https://gateway.example.invalid/gemini",
            "api_key": "gemini-key",
            "timeout_sec": 10,
        }

        with patch.object(
            ai,
            "_get_json",
            return_value={
                "models": [
                    {
                        "name": "models/gemini-3.5-flash-lite",
                        "displayName": "Gemini 3.5 Flash-Lite",
                        "supportedGenerationMethods": [
                            "generateContent",
                            "countTokens",
                        ],
                        "inputTokenLimit": 1000000,
                    },
                    {
                        "name": "models/gemini-2.0-flash-lite",
                        "displayName": "Gemini 2.0 Flash-Lite",
                        "supportedGenerationMethods": ["generateContent"],
                    },
                    {
                        "name": "models/gemini-embedding-001",
                        "supportedGenerationMethods": ["embedContent"],
                    },
                ]
            },
        ) as get_json:
            result = ai._list_provider_models(ai_config)

        self.assertEqual(result["provider"], "google_gemini")
        self.assertEqual(
            [model["name"] for model in result["models"]],
            ["gemini-2.0-flash-lite", "gemini-3.5-flash-lite"],
        )
        retired = result["models"][0]
        self.assertEqual(retired["source"], "external")
        self.assertEqual(retired["source_detail"], "google")
        self.assertEqual(retired["lifecycle"]["status"], "retired")
        self.assertEqual(retired["lifecycle"]["replacement"], "gemini-3.1-flash-lite")
        self.assertNotIn("lifecycle", result["models"][1])
        get_json.assert_called_once_with(
            "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000",
            {
                "x-goog-api-key": "gemini-key",
                "content-type": "application/json",
            },
            10,
        )

    def test_retired_gemini_model_is_rejected_before_provider_call(self):
        with (
            patch.object(ai, "_post_json") as post_json,
            self.assertRaisesRegex(
                ai.HTTPException,
                "retired Gemini model gemini-2.0-flash-lite",
            ),
        ):
            ai._call_ai_messages_result(
                {
                    "provider": "gemini",
                    "model": "gemini-2.0-flash-lite",
                    "api_key": "gemini-key",
                },
                [{"role": "user", "content": "test"}],
            )

        post_json.assert_not_called()

    def test_gemini_requires_api_key(self):
        with self.assertRaisesRegex(
            ai.HTTPException, "Google Gemini API key is not configured"
        ):
            ai._call_ai_messages_result(
                {
                    "provider": "gemini",
                    "model": "gemini-3.5-flash-lite",
                    "api_key": "",
                },
                [{"role": "user", "content": "test"}],
            )

    def test_call_ai_messages_result_returns_ollama_counts(self):
        ai_config = {
            "provider": "ollama",
            "base_url": "http://ollama:11434",
            "model": "llama3.1",
        }

        with patch.object(
            ai,
            "_post_json",
            return_value={
                "message": {"content": "ok"},
                "prompt_eval_count": 11,
                "eval_count": 4,
                "total_duration": 123,
            },
        ):
            result = ai._call_ai_messages_result(
                ai_config, [{"role": "user", "content": "test"}]
            )

        self.assertEqual(result["content"], "ok")
        self.assertEqual(result["usage"]["prompt_tokens"], 11)
        self.assertEqual(result["usage"]["completion_tokens"], 4)
        self.assertEqual(result["usage"]["total_tokens"], 15)
        self.assertEqual(result["usage"]["total_duration"], 123)

    def test_provider_test_uses_short_message_call(self):
        ai_config = {
            "provider": "ollama",
            "base_url": "http://ollama:11434",
            "model": "llama3.1",
        }

        with patch.object(
            ai,
            "_call_ai_messages_result",
            return_value={
                "content": "Provider works.",
                "usage": {"prompt_tokens": 4, "completion_tokens": 2},
            },
        ) as call:
            result = ai._provider_test(ai_config)

        call.assert_called_once()
        self.assertEqual(call.call_args.kwargs["max_tokens"], 120)
        self.assertTrue(result["ok"])
        self.assertEqual(result["response"], "Provider works.")
        self.assertEqual(result["usage"]["prompt_tokens"], 4)

    def test_provider_error_detail_prefers_detail_message(self):
        self.assertEqual(
            ai._provider_error_detail({"detail": "payload too large"}),
            "payload too large",
        )
        self.assertEqual(
            ai._provider_error_detail({"error": {"message": "bad model"}}),
            "bad model",
        )

    def test_stack_bundle_is_compacted_for_provider(self):
        bundle = {
            "generated_at": "2026-06-18T00:00:00Z",
            "scope": "stack",
            "question": "What services should I use for Usenet?",
            "stack_summary": {
                "counts": {"enabled": 20},
                "attention": {
                    "unhealthy": [{"process_name": f"bad-{idx}"} for idx in range(8)],
                    "stopped": [],
                    "unknown": [],
                },
            },
            "processes": [{"process_name": f"service-{idx}"} for idx in range(50)],
            "dependency_graph": {
                "nodes": [{"id": f"node-{idx}"} for idx in range(80)],
                "edges": [{"source": "a", "target": f"b-{idx}"} for idx in range(90)],
            },
            "logs": {"bad": {"content": "x" * 5000}},
            "docs_context": {
                "available": True,
                "sources": [
                    {
                        "title": "NzbDAV",
                        "path": "services/core/nzbdav.md",
                        "url": "https://dumbarr.com/services/core/nzbdav/",
                        "source": "web",
                        "excerpt": "y" * 5000,
                    }
                ],
            },
            "service_configs": {"bad": {"large": "z" * 5000}},
            "dumb_service_catalog": ai.DUMB_SERVICE_CATALOG,
            "dumb_workflow_rules": ai.DUMB_WORKFLOW_RULES,
        }

        compact = ai._bundle_for_provider(bundle)

        self.assertEqual(compact["scope"], "stack")
        self.assertEqual(compact["analysis_mode"], "workflow_planning")
        self.assertIn("usenet_workflows", compact["dumb_service_catalog"])
        self.assertIn(
            "SABnzbd", compact["dumb_workflow_rules"]["not_primary_dumb_services"]
        )
        self.assertEqual(len(compact["stack_summary"]["attention"]["unhealthy"]), 5)
        self.assertEqual(len(compact["processes"]), 30)
        self.assertEqual(len(compact["dependency_graph"]["nodes"]), 40)
        self.assertEqual(len(compact["dependency_graph"]["edges"]), 60)
        self.assertNotIn("logs", compact)
        self.assertIn("logs_note", compact)
        self.assertLess(
            len(compact["docs_context"]["sources"][0]["excerpt"]),
            1200,
        )
        self.assertIn("service_configs_note", compact)

    def test_diagnostic_messages_include_dumb_workflow_guardrails(self):
        messages = ai._diagnostic_messages(
            {
                "scope": "stack",
                "question": "What services should I use for Usenet?",
                "dumb_product": ai.DUMB_PRODUCT_FACTS,
                "dumb_service_catalog": ai.DUMB_SERVICE_CATALOG,
                "dumb_workflow_rules": ai.DUMB_WORKFLOW_RULES,
            }
        )

        system = messages[0]["content"]
        user = messages[1]["content"]
        self.assertIn("Distributed Unlimited Media Bridge", system)
        self.assertIn("DUMB PRODUCT FACTS", user)
        self.assertIn("Docker Universal Media Box", user)
        self.assertIn("dumb_service_catalog", system)
        self.assertIn("Decypharr, NzbDAV, AltMount", system)
        self.assertIn("external SABnzbd", system)
        self.assertIn("NZBGet", system)
        self.assertIn("CRITICAL DUMB WORKFLOW RULES", user)
        self.assertIn("NZBHydra", user)

    def test_usenet_stack_finalizer_replaces_generic_external_client_answer(self):
        bundle = {
            "scope": "stack",
            "question": "What services should I use for Usenet?",
            "processes": [
                {
                    "name": "Prowlarr",
                    "process_name": "Prowlarr",
                    "config_key": "prowlarr",
                    "status": "stopped",
                },
                {
                    "name": "Sonarr",
                    "process_name": "Sonarr",
                    "config_key": "sonarr",
                    "status": "running",
                },
            ],
        }
        generic = (
            "I recommend NZBGet or SABnzbd as your download client. "
            "Install one and configure your Usenet provider."
        )

        finalized = ai._finalize_stack_analysis(bundle, generic)

        self.assertIn("Decypharr", finalized)
        self.assertIn("NzbDAV", finalized)
        self.assertIn("AltMount", finalized)
        self.assertIn("Prowlarr: stopped", finalized)
        self.assertNotIn("Install one and configure", finalized)

    def test_usenet_stack_finalizer_keeps_good_provider_notes_after_canonical_answer(
        self,
    ):
        bundle = {
            "scope": "stack",
            "question": "What services should I use for Usenet?",
            "processes": [],
        }
        provider = "Decypharr is a good DUMB-native fit."

        finalized = ai._finalize_stack_analysis(bundle, provider)

        self.assertTrue(finalized.startswith("## Direct Answer"))
        self.assertIn("## Provider Notes", finalized)
        self.assertIn(provider, finalized)

    def test_stack_finalizer_replaces_invented_dumb_acronym(self):
        bundle = {
            "scope": "stack",
            "question": "What does DUMB stand for?",
            "processes": [],
        }
        provider = "DUMB stands for Decentralized Usenet Media Butler."

        finalized = ai._finalize_stack_analysis(bundle, provider)

        self.assertIn("Distributed Unlimited Media Bridge", finalized)
        self.assertIn("Do not use other acronym expansions", finalized)
        self.assertNotIn("Decentralized Usenet Media Butler", finalized)

    def test_product_identity_question_detection_is_specific_to_dumb(self):
        self.assertTrue(ai._is_product_identity_question("What does DUMB stand for?"))
        self.assertTrue(ai._is_product_identity_question("What is DUMB?"))
        self.assertFalse(ai._is_product_identity_question("What does API stand for?"))
        self.assertFalse(
            ai._is_product_identity_question("What services should I use?")
        )

    def test_docs_context_selects_service_and_relevant_docs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            docs_root = Path(tmpdir)
            (docs_root / "index.md").write_text("# DUMB Docs\n")
            for relative, content in {
                "services/core/decypharr.md": "# Decypharr\nMount and WebDAV notes.",
                "features/embedded-ui.md": "# Embedded UI\nProxy iframe routing.",
                "frontend/service-pages.md": "# Service Pages\nLogs and config.",
                "api/process.md": "# Process API\nDependency graph.",
            }.items():
                path = docs_root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)

            bundle = {
                "process_name": "Decypharr",
                "config_key": "decypharr",
                "service_path": "decypharr",
                "question": "Why is the embedded UI proxy failing?",
                "service_status": {"status": "running"},
                "logs": {"content": "iframe proxy route failed"},
            }
            request = ai.AiDiagnosticRequest(process_name="Decypharr")

            with patch.object(ai, "_docs_root_candidates", return_value=[docs_root]):
                context = ai._build_docs_context(
                    bundle,
                    {**ai.DEFAULT_AI_CONFIG, "max_docs_chars": 4000},
                    request,
                )

        self.assertTrue(context["available"])
        paths = [source["path"] for source in context["sources"]]
        self.assertIn("services/core/decypharr.md", paths)
        self.assertIn("features/embedded-ui.md", paths)

    def test_docs_context_falls_back_to_public_docs(self):
        bundle = {
            "scope": "stack",
            "question": "What is blocking startup?",
            "stack_summary": {"counts": {"unhealthy": 1}},
            "logs": {"PostgreSQL": {"content": "database startup failed"}},
        }
        request = ai.AiStackDiagnosticRequest(question="What is blocking startup?")
        fake_response = SimpleNamespace(
            status_code=200,
            text="<html><body><main><h1>Backend</h1><p>Startup orchestration docs.</p></main></body></html>",
        )

        with (
            patch.object(ai, "_find_docs_root", return_value=None),
            patch.object(ai.requests, "get", return_value=fake_response) as get,
        ):
            context = ai._build_docs_context(
                bundle,
                {**ai.DEFAULT_AI_CONFIG, "max_docs_chars": 2000},
                request,
            )

        self.assertTrue(context["available"])
        self.assertEqual(context["source"], "web")
        self.assertEqual(context["sources"][0]["source"], "web")
        self.assertIn("https://dumbarr.com/", get.call_args.args[0])

    def test_public_docs_context_keeps_article_and_normalizes_whitespace(self):
        rendered_page = """
        <html>
          <head><style>.hidden { display: none; }</style></head>
          <body>
            <header>Global header</header>
            <nav>Documentation navigation</nav>
            <main>
              <aside>On this page</aside>
              <article>
                <h1>AI Assistant</h1>


                <p>
                  Use retained logs &amp; metrics.
                </p>
                <ul>
                  <li>Preview the bundle</li>
                  <li>Review evidence</li>
                </ul>
              </article>
            </main>
            <footer>Site footer</footer>
            <script>window.secret = "not context";</script>
          </body>
        </html>
        """

        normalized = ai._normalize_doc_text(rendered_page, rendered_html=True)

        self.assertIn("AI Assistant", normalized)
        self.assertIn("Use retained logs & metrics.", normalized)
        self.assertIn("Preview the bundle", normalized)
        self.assertNotIn("Documentation navigation", normalized)
        self.assertNotIn("On this page", normalized)
        self.assertNotIn("window.secret", normalized)
        self.assertNotRegex(normalized, r"\n{3,}")
        self.assertFalse(any(line.isspace() for line in normalized.splitlines()))

    def test_docs_candidates_include_bundled_snapshot(self):
        with patch.dict(ai.os.environ, {}, clear=True):
            candidates = ai._docs_root_candidates()

        self.assertIn(Path("/usr/share/dumb/docs"), candidates)

    def test_docs_context_selects_usenet_workflow_docs_for_planning_question(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            docs_root = Path(tmpdir)
            (docs_root / "index.md").write_text("# DUMB Docs\n")
            for relative, content in {
                "reference/core-service.md": "# Core Service Routing\nUse nzbdav or altmount for Usenet workflows.",
                "services/core/nzbdav.md": "# NzbDAV\nUsenet WebDAV and Arr download-client integration.",
                "services/core/altmount.md": "# AltMount\nAlternative Usenet workflow.",
                "services/core/decypharr.md": "# Decypharr\nDebrid and native Usenet workflow.",
                "features/index.md": "# Features\nDebrid and Usenet services.",
            }.items():
                path = docs_root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)

            bundle = {
                "scope": "stack",
                "question": "What services should I use if I want to use Usenet?",
                "stack_summary": {"counts": {"enabled": 4}},
                "logs": {},
            }
            request = ai.AiStackDiagnosticRequest(question=bundle["question"])

            with patch.object(ai, "_docs_root_candidates", return_value=[docs_root]):
                context = ai._build_docs_context(
                    bundle,
                    {**ai.DEFAULT_AI_CONFIG, "max_docs_chars": 4000},
                    request,
                )

        paths = [source["path"] for source in context["sources"]]
        self.assertIn("reference/core-service.md", paths)
        self.assertIn("services/core/nzbdav.md", paths)
        self.assertIn("services/core/altmount.md", paths)

    def test_build_diagnostic_bundle_redacts_config_and_tails_logs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "service.log"
            log_path.write_text("INFO ok\nERROR token=raw-secret\n")
            config = {
                "demo": {
                    "process_name": "Demo",
                    "api_key": "raw-secret",
                    "log_file": str(log_path),
                }
            }
            api_state = SimpleNamespace(
                get_status_details=lambda process_name, include_health=False: {
                    "status": "running"
                },
                get_status=lambda process_name: "running",
            )
            logger = SimpleNamespace(debug=lambda *a, **k: None)
            request = ai.AiDiagnosticRequest(
                process_name="Demo",
                include_dependency_graph=False,
                max_log_chars=1000,
                dry_run=True,
            )

            with (
                patch.object(ai.CONFIG_MANAGER, "config", config),
                patch.object(
                    ai.CONFIG_MANAGER,
                    "find_key_for_process",
                    return_value=("demo", None),
                ),
            ):
                bundle = ai._build_diagnostic_bundle(
                    request, ai.DEFAULT_AI_CONFIG, api_state, logger, "user"
                )

        self.assertEqual(bundle["service_config"]["api_key"], "[REDACTED]")
        self.assertIn("token=[REDACTED]", bundle["logs"]["content"])
        self.assertEqual(bundle["service_status"]["status"], "running")

    def test_build_stack_diagnostic_bundle_includes_whole_stack_context(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "bad.log"
            log_path.write_text("ERROR postgres connection refused token=raw-secret\n")
            docs_root = Path(tmpdir) / "docs"
            docs_root.mkdir()
            (docs_root / "index.md").write_text("# DUMB Docs\n")
            docs_file = docs_root / "services/dependent/postgres.md"
            docs_file.parent.mkdir(parents=True)
            docs_file.write_text("# PostgreSQL\nDatabase startup guidance.")

            processes = [
                {
                    "name": "Good",
                    "process_name": "Good Service",
                    "config_key": "good",
                    "enabled": True,
                    "version": "1.0.0",
                    "config": {"process_name": "Good Service"},
                },
                {
                    "name": "Postgres",
                    "process_name": "PostgreSQL",
                    "config_key": "postgres",
                    "enabled": True,
                    "version": "16",
                    "config": {
                        "process_name": "PostgreSQL",
                        "password": "raw-secret",
                    },
                },
            ]
            api_state = SimpleNamespace(
                get_status_details=lambda process_name, include_health=False: (
                    {
                        "status": "stopped",
                        "healthy": False,
                        "health_reason": "connection refused",
                    }
                    if process_name == "PostgreSQL"
                    else {"status": "running", "healthy": True}
                ),
                get_status=lambda process_name: (
                    "stopped" if process_name == "PostgreSQL" else "running"
                ),
            )
            logger = SimpleNamespace(debug=lambda *a, **k: None)
            request = ai.AiStackDiagnosticRequest(
                question="What is broken in the stack?",
                include_service_config=True,
                include_dependency_graph=True,
                include_docs_context=True,
                max_log_chars=2000,
            )

            with (
                patch.object(ai, "_collect_process_entries", return_value=processes),
                patch.object(ai, "_docs_root_candidates", return_value=[docs_root]),
                patch.object(ai, "find_log_file", return_value=log_path),
                patch.object(
                    ai,
                    "dependency_graph",
                    return_value={
                        "nodes": [{"id": "PostgreSQL"}],
                        "edges": [],
                    },
                ),
            ):
                bundle = ai._build_stack_diagnostic_bundle(
                    request, ai.DEFAULT_AI_CONFIG, api_state, logger, "user"
                )

        self.assertEqual(bundle["scope"], "stack")
        self.assertEqual(
            bundle["dumb_product"]["expansion"], "Distributed Unlimited Media Bridge"
        )
        self.assertEqual(bundle["stack_summary"]["counts"]["enabled"], 2)
        self.assertEqual(bundle["stack_summary"]["counts"]["unhealthy"], 1)
        self.assertNotIn("services", bundle["stack_summary"])
        self.assertIn("usenet_workflows", bundle["dumb_service_catalog"])
        self.assertIn("not_primary_dumb_services", bundle["dumb_workflow_rules"])
        self.assertEqual(len(bundle["processes"]), 2)
        self.assertIn("PostgreSQL", bundle["logs"])
        self.assertIn("token=[REDACTED]", bundle["logs"]["PostgreSQL"]["content"])
        self.assertEqual(
            bundle["service_configs"]["PostgreSQL"]["password"], "[REDACTED]"
        )
        self.assertTrue(bundle["dependency_graph"]["nodes"])
        self.assertTrue(bundle["docs_context"]["available"])


if __name__ == "__main__":
    unittest.main()
