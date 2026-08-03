import unittest

from scripts.check_ai_model_lifecycle import (
    compare_catalog,
    parse_lifecycle_rows,
)
from utils.ai_model_catalog import model_compatibility, model_lifecycle


class AiModelCatalogTests(unittest.TestCase):
    def test_openai_parser_reads_upcoming_and_past_text_model_rows(self):
        html = """
        <h2 id="upcoming-deprecations">Upcoming</h2>
        <table>
          <tr><th>Shutdown date</th><th>Model / system</th><th>Recommended replacement</th></tr>
          <tr><td>Aug 10, 2026</td><td><code>gpt-old</code></td><td><code>gpt-new</code></td></tr>
          <tr><td>Aug 10, 2026</td><td><code>gpt-image-old</code></td><td><code>gpt-image-new</code></td></tr>
        </table>
        <h2 id="past-deprecations">Past</h2>
        <table>
          <tr><th>Shutdown date</th><th>Model</th><th>Recommended replacement</th></tr>
          <tr><td>Jan 1, 2020</td><td><code>gpt-ancient</code></td><td><code>gpt-new</code></td></tr>
        </table>
        """

        self.assertEqual(
            parse_lifecycle_rows("openai", html),
            [
                {
                    "model": "gpt-old",
                    "shutdown_date": "2026-08-10",
                    "replacement": "gpt-new",
                },
                {
                    "model": "gpt-ancient",
                    "shutdown_date": "2020-01-01",
                    "replacement": "gpt-new",
                },
            ],
        )

    def test_google_parser_ignores_models_without_shutdown_dates(self):
        html = """
        <table>
          <tr><th>Model</th><th>Release date</th><th>Shutdown date</th><th>Recommended replacement</th></tr>
          <tr><td><code>gemini-old</code></td><td>May 1, 2026</td><td>October 16, 2026</td><td><code>gemini-new</code></td></tr>
          <tr><td><code>gemini-current</code></td><td>May 2, 2026</td><td>No shutdown date announced</td><td></td></tr>
        </table>
        """

        self.assertEqual(
            parse_lifecycle_rows("google_gemini", html),
            [
                {
                    "model": "gemini-old",
                    "shutdown_date": "2026-10-16",
                    "replacement": "gemini-new",
                }
            ],
        )

    def test_google_models_without_shutdown_dates_have_no_lifecycle_warning(self):
        self.assertIsNone(model_lifecycle("google_gemini", "gemini-2.5-pro"))
        self.assertIsNone(model_lifecycle("google_gemini", "gemini-2.5-flash"))
        self.assertIsNone(model_lifecycle("google_gemini", "gemini-2.5-flash-lite"))

    def test_catalog_comparison_reports_new_or_changed_models(self):
        observed = [
            {
                "model": "gpt-old",
                "shutdown_date": "2026-08-10",
                "replacement": "gpt-new",
            },
            {
                "model": "gpt-untracked",
                "shutdown_date": "2026-09-01",
                "replacement": "gpt-new",
            },
        ]
        errors = compare_catalog(
            "openai",
            {
                "gpt-old": {
                    "shutdown_date": "2026-08-11",
                    "replacement": "gpt-new",
                }
            },
            observed,
        )

        self.assertTrue(any("official source added" in error for error in errors))
        self.assertTrue(any("shutdown is 2026-08-10" in error for error in errors))

    def test_catalog_comparison_ignores_untracked_historical_models(self):
        errors = compare_catalog(
            "openai",
            {
                "gpt-retained": {
                    "shutdown_date": "2026-07-23",
                    "replacement": "gpt-new",
                }
            },
            [
                {
                    "model": "gpt-retained",
                    "shutdown_date": "2026-07-23",
                    "replacement": "gpt-new",
                },
                {
                    "model": "gpt-untracked-history",
                    "shutdown_date": "2020-01-01",
                    "replacement": "gpt-new",
                },
            ],
            as_of="2026-08-03",
        )

        self.assertEqual(errors, [])

    def test_provider_metadata_distinguishes_retirement_from_compatibility(self):
        lifecycle = model_lifecycle("openai", "gpt-5-codex", "2026-07-24")
        current = model_compatibility("openai", "gpt-5.3-codex")
        unsupported = model_compatibility("openai", "text-embedding-3-small")

        self.assertEqual(lifecycle["status"], "retired")
        self.assertEqual(current["status"], "supported")
        self.assertEqual(current["api_surface"], "responses")
        self.assertEqual(unsupported["status"], "unsupported")


if __name__ == "__main__":
    unittest.main()
