from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from config import load_config


BASE_CONFIG = """
email_provider = "duckmail"

[duckmail]
domain = "duckmail.sbs"

[captcha]
{captcha_settings}
"""


class CaptchaConfigTests(unittest.TestCase):
    def _load(self, captcha_settings: str):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(
                BASE_CONFIG.format(captcha_settings=captcha_settings),
                encoding="utf-8",
            )
            with patch("config.CONFIG_FILE", path):
                return load_config()

    def test_existing_manual_config_uses_llm_defaults(self) -> None:
        config = self._load('mode = "manual"')

        self.assertEqual(config.captcha.mode, "manual")
        self.assertIsNone(config.captcha.llm_model)
        self.assertIsNone(config.captcha.llm_api_key)
        self.assertEqual(config.captcha.llm_api_base, "https://api.openai.com/v1")
        self.assertEqual(config.captcha.llm_calls_per_attempt, 8)
        self.assertEqual(config.captcha.llm_max_attempts, 2)
        self.assertEqual(config.captcha.llm_max_output_tokens, 1200)
        self.assertEqual(config.captcha.llm_max_concurrency, 1)
        self.assertIsNone(config.captcha.llm_artifact_dir)
        self.assertEqual(config.browser.launch_stagger_seconds, 8)

    def test_loads_llm_mode_without_changing_other_modes(self) -> None:
        config = self._load(
            """
mode = "llm"
llm_model = "vision-model"
llm_api_base = "https://api.example.test/v1/"
llm_api_key = "secret"
llm_reasoning_effort = "high"
llm_call_delay_seconds = 1
llm_action_delay_seconds = 2
llm_calls_per_attempt = 3
llm_max_attempts = 4
llm_max_output_tokens = 2048
llm_artifact_dir = "debug/captcha"
"""
        )

        self.assertEqual(config.captcha.mode, "llm")
        self.assertEqual(config.captcha.llm_model, "vision-model")
        self.assertEqual(config.captcha.llm_api_base, "https://api.example.test/v1")
        self.assertEqual(config.captcha.llm_api_key, "secret")
        self.assertEqual(config.captcha.llm_reasoning_effort, "high")
        self.assertEqual(config.captcha.llm_call_delay_seconds, 1)
        self.assertEqual(config.captcha.llm_action_delay_seconds, 2)
        self.assertEqual(config.captcha.llm_calls_per_attempt, 3)
        self.assertEqual(config.captcha.llm_max_attempts, 4)
        self.assertEqual(config.captcha.llm_max_output_tokens, 2048)
        self.assertTrue(str(config.captcha.llm_artifact_dir).endswith("debug/captcha"))

    def test_llm_mode_requires_model_and_api_key(self) -> None:
        with self.assertRaisesRegex(ValueError, "llm_model.*llm_api_key"):
            self._load('mode = "llm"')

    def test_rejects_llm_execution_parameters_outside_limits(self) -> None:
        with self.assertRaisesRegex(ValueError, "llm_calls_per_attempt"):
            self._load(
                """
mode = "manual"
llm_calls_per_attempt = 0
"""
            )

        with self.assertRaisesRegex(ValueError, "llm_max_output_tokens"):
            self._load(
                """
mode = "manual"
llm_max_output_tokens = 64
"""
            )

    def test_browser_concurrency_defaults_to_one_and_is_validated(self) -> None:
        self.assertEqual(self._load('mode = "manual"').browser.concurrency, 1)

        with self.assertRaisesRegex(ValueError, "browser.concurrency"):
            self._load('mode = "manual"\n\n[browser]\nconcurrency = 11')

        with self.assertRaisesRegex(ValueError, "launch_stagger_seconds"):
            self._load(
                'mode = "manual"\n\n[browser]\nlaunch_stagger_seconds = 301'
            )


if __name__ == "__main__":
    unittest.main()
