import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from api.routers import ai


class AiRouterTests(unittest.TestCase):
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
        public = ai._public_settings({"api_key": "sk-test", "enabled": True})

        self.assertNotIn("api_key", public)
        self.assertTrue(public["api_key_configured"])
        self.assertTrue(public["enabled"])

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
            model="gpt-4.1-mini",
            api_key="",
        )

        with patch.object(ai.CONFIG_MANAGER, "config", config):
            effective = ai._effective_ai_config(request)

        self.assertEqual(effective["api_key"], "stored-key")

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
        self.assertIn("Debrid Unlimited Media Bridge", system)
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

        self.assertIn("Debrid Unlimited Media Bridge", finalized)
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
            bundle["dumb_product"]["expansion"], "Debrid Unlimited Media Bridge"
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
