from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from config import (
    AppConfig,
    BrowserConfig,
    CaptchaConfig,
    CloudflareTempEmailConfig,
    DuckMailConfig,
    NvidiaConfig,
)
from main import (
    _chromium_user_agent,
    _click_register_and_wait_result,
    _login_ngc,
    _parse_cli_options,
    _recover_from_navigation_error,
    _redact_artifact_url,
    _request_new_verification_code,
    _run_accounts,
    _skip_passkey_prompt,
    _skip_passkey_prompt_if_present,
    _solve_captcha_and_submit,
    _wait_for_register_response,
    _wait_for_url_change,
    _wait_for_verification_submission,
    run,
)


class CliOptionsTests(unittest.TestCase):
    def test_parses_count_and_headless_override(self) -> None:
        self.assertEqual(
            _parse_cli_options(["main.py", "--headless", "-n", "3"]),
            (3, True, None),
        )

    def test_last_browser_mode_flag_wins(self) -> None:
        self.assertEqual(
            _parse_cli_options(["main.py", "--headless", "--headed"]),
            (None, False, None),
        )

    def test_parses_concurrency_override(self) -> None:
        self.assertEqual(
            _parse_cli_options(["main.py", "-n", "10", "-j", "3"]),
            (10, None, 3),
        )

    def test_headless_user_agent_uses_browser_version_without_marker(self) -> None:
        user_agent = _chromium_user_agent("138.0.7204.23")

        self.assertIn("Chrome/138.0.7204.23", user_agent)
        self.assertNotIn("HeadlessChrome", user_agent)

    def test_failure_artifact_url_drops_sensitive_query_and_fragment(self) -> None:
        redacted = _redact_artifact_url(
            "https://login.example.test/v1/create-account?email=user@example.test&key=secret#step"
        )

        self.assertEqual(redacted, "https://login.example.test/v1/create-account")


class HeadlessModeTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejects_manual_captcha_before_starting_browser(self) -> None:
        config = _app_config("manual")

        with self.assertRaisesRegex(ValueError, "automatic captcha mode"):
            await run(config, 1)

    async def test_accepts_llm_mode_validation(self) -> None:
        config = _app_config("llm")
        # A zero count avoids external I/O while exercising the startup validation path.
        await run(config, 0)


class VerificationSubmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejects_explicit_invalid_code_message(self) -> None:
        body = Mock()
        body.inner_text = AsyncMock(return_value="验证码无效。请重新请求验证码。")
        page = Mock(url="https://login.example.test/v1/profile-complete")
        page.locator.return_value = body

        accepted = await _wait_for_verification_submission(page, page.url, 1)

        self.assertFalse(accepted)

    async def test_accepts_when_verification_inputs_disappear(self) -> None:
        body = Mock()
        body.inner_text = AsyncMock(return_value="Processing")
        inputs = Mock()
        inputs.count = AsyncMock(return_value=0)
        page = Mock(url="https://login.example.test/v1/profile-complete")
        page.locator.side_effect = lambda selector: body if selector == "body" else inputs

        accepted = await _wait_for_verification_submission(page, page.url, 1)

        self.assertTrue(accepted)

    async def test_ngc_login_advances_email_then_password(self) -> None:
        original_url = "https://ngc.nvidia.com/signin"
        page = Mock(url=original_url)

        password = Mock()
        password.count = AsyncMock(side_effect=[0, 1, 0])
        password.is_visible = AsyncMock(return_value=True)
        password.fill = AsyncMock()
        password.first = password

        email = Mock()
        email.count = AsyncMock(side_effect=[1, 0])
        email.is_visible = AsyncMock(return_value=True)
        email.fill = AsyncMock()
        email.first = email

        continue_button = Mock()
        continue_button.count = AsyncMock(return_value=1)
        continue_button.is_visible = AsyncMock(return_value=True)
        continue_button.is_enabled = AsyncMock(return_value=True)
        continue_button.click = AsyncMock()
        continue_button.first = continue_button

        login_button = Mock()
        login_button.count = AsyncMock(return_value=1)
        login_button.is_visible = AsyncMock(return_value=True)
        login_button.is_enabled = AsyncMock(return_value=True)

        async def finish_login(**_kwargs) -> None:
            page.url = "https://ngc.nvidia.com/"

        login_button.click = AsyncMock(side_effect=finish_login)
        login_button.first = login_button

        page.locator.side_effect = (
            lambda selector: password if 'type="password"' in selector else email
        )
        page.get_by_role.side_effect = lambda _role, name: (
            login_button if name == "Log In" else continue_button
        )

        with patch("main.asyncio.sleep", new=AsyncMock()):
            logged_in = await _login_ngc(page, "user@example.test", "secret")

        self.assertTrue(logged_in)
        email.fill.assert_awaited_once_with("user@example.test")
        password.fill.assert_awaited_once_with("secret")


class ParallelSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_pool_limits_concurrency_and_runs_every_account(self) -> None:
        config = _app_config("llm", concurrency=2)
        active = 0
        maximum_active = 0
        account_indices: list[int] = []

        async def fake_register(*_args, account_index: int, **_kwargs):
            nonlocal active, maximum_active
            active += 1
            maximum_active = max(maximum_active, active)
            account_indices.append(account_index)
            await asyncio.sleep(0.01)
            active -= 1
            return f"key-{account_index}"

        with patch("main._register_one", side_effect=fake_register):
            results = await _run_accounts(object(), config, 5)

        self.assertEqual(maximum_active, 2)
        self.assertEqual(sorted(account_indices), [0, 1, 2, 3, 4])
        self.assertEqual(sorted(results), [f"key-{index}" for index in range(5)])


class PasskeyPromptTests(unittest.IsolatedAsyncioTestCase):
    def _cancel_button_page(self, cancel_count: int = 1) -> tuple[object, Mock]:
        """Build a page whose #cancelSetupSelect_btn exists and whose named
        buttons (confirm dialog) are all clickable."""
        cancel = Mock()
        cancel.count = AsyncMock(return_value=cancel_count)
        cancel.first = cancel
        cancel.click = AsyncMock()

        confirm = Mock()
        confirm.count = AsyncMock(return_value=1)
        confirm.is_enabled = AsyncMock(return_value=True)
        confirm.click = AsyncMock()
        confirm.filter = Mock(return_value=confirm)
        confirm.first = confirm

        page = Mock()
        page.locator.return_value = cancel
        page.get_by_role.return_value = confirm
        return page, cancel

    async def test_skip_prompt_clicks_cancel_then_confirms(self) -> None:
        page, cancel = self._cancel_button_page()

        with patch("main.asyncio.sleep", new=AsyncMock()):
            skipped = await _skip_passkey_prompt(page)

        self.assertTrue(skipped)
        cancel.click.assert_awaited_once()
        page.get_by_role().click.assert_awaited()

    async def test_skip_prompt_polls_until_passkey_url_appears(self) -> None:
        page, cancel = self._cancel_button_page()
        page.url = "https://login.nvgs.nvidia.com/v1/profile-complete"

        async def fake_sleep(_seconds: float) -> None:
            page.url = "https://login.nvgs.nvidia.com/v1/passkey/prompt-setup?x=1"

        with patch("main.asyncio.sleep", new=AsyncMock(side_effect=fake_sleep)):
            skipped = await _skip_passkey_prompt_if_present(page, wait_seconds=15)

        self.assertTrue(skipped)
        cancel.click.assert_awaited_once()

    async def test_skip_prompt_returns_false_when_never_present(self) -> None:
        page = Mock()
        page.url = "https://login.nvgs.nvidia.com/v1/signin-redirect"
        page.locator.return_value.count = AsyncMock(return_value=0)
        page.get_by_role.return_value.count = AsyncMock(return_value=0)
        page.get_by_role.return_value.is_enabled = AsyncMock(return_value=False)

        with patch("main.asyncio.sleep", new=AsyncMock()):
            skipped = await _skip_passkey_prompt_if_present(page, wait_seconds=0.05)

        self.assertFalse(skipped)

    async def test_skip_prompt_exits_early_after_moving_past_passkey(self) -> None:
        page = Mock()
        page.url = "https://login.nvgs.nvidia.com/v1/consent"
        page.locator.return_value.count = AsyncMock(return_value=0)

        with patch("main.asyncio.sleep", new=AsyncMock()) as mocked_sleep:
            skipped = await _skip_passkey_prompt_if_present(page, wait_seconds=30)

        self.assertFalse(skipped)
        mocked_sleep.assert_not_awaited()

    async def test_wait_for_url_change_detects_navigation(self) -> None:
        page = Mock()
        page.url = "https://login.nvgs.nvidia.com/v1/passkey/prompt-setup"

        async def fake_sleep(_seconds: float) -> None:
            page.url = "https://login.nvgs.nvidia.com/v1/signin-redirect"

        with patch("main.asyncio.sleep", new=AsyncMock(side_effect=fake_sleep)):
            changed = await _wait_for_url_change(page, page.url, wait_seconds=5)

        self.assertTrue(changed)

    async def test_wait_for_url_change_times_out(self) -> None:
        page = Mock()
        page.url = "https://login.nvgs.nvidia.com/v1/passkey/prompt-setup"

        with patch("main.asyncio.sleep", new=AsyncMock()):
            changed = await _wait_for_url_change(page, page.url, wait_seconds=0.05)

        self.assertFalse(changed)


class RobustnessTests(unittest.IsolatedAsyncioTestCase):
    def _register_page(self, response_statuses: list[int]) -> tuple[object, object, list[Mock]]:
        """Page whose #register_button is enabled and whose register API responses
        come back with the given statuses, in order."""
        button = Mock()
        button.wait_for = AsyncMock()
        button.is_enabled = AsyncMock(return_value=True)
        button.click = AsyncMock()

        responses = []
        for status in response_statuses:
            resp = Mock()
            resp.status = status
            resp.url = "https://login.nvgs.nvidia.com/api/1/frontend/oauth/user/register"
            resp.request.method = "POST"
            resp.text = AsyncMock(return_value="{}")
            responses.append(resp)

        page = Mock()
        page.locator.return_value = button
        page.wait_for_event = AsyncMock(side_effect=responses)
        page.evaluate = AsyncMock(return_value=True)
        return page, button, responses

    async def test_register_response_accepted(self) -> None:
        page, _, _ = self._register_page([200])
        result = await _wait_for_register_response(page)
        self.assertEqual(result, "accepted")

    async def test_register_response_email_exists(self) -> None:
        page, _, responses = self._register_page([409])
        responses[0].text = AsyncMock(return_value='{"error": "CONFLICT"}')
        result = await _wait_for_register_response(page)
        self.assertEqual(result, "email_exists")

    async def test_register_response_rejected(self) -> None:
        page, _, _ = self._register_page([500])
        result = await _wait_for_register_response(page)
        self.assertEqual(result, "rejected")

    async def test_click_register_and_wait_result(self) -> None:
        page, button, _ = self._register_page([200])
        with patch("main.asyncio.sleep", new=AsyncMock()):
            result = await _click_register_and_wait_result(page)
        self.assertEqual(result, "accepted")
        button.click.assert_awaited_once()

    async def test_solve_captcha_and_submit_retries_on_rejection(self) -> None:
        page, _, _ = self._register_page([500, 200])
        solver = Mock()
        solver.solve = AsyncMock(return_value=True)
        config = _app_config("llm")

        with patch("main.asyncio.sleep", new=AsyncMock()):
            ok = await _solve_captcha_and_submit(page, solver, config)

        self.assertTrue(ok)
        self.assertEqual(solver.solve.await_count, 2)
        # 第二次尝试前重置了 hCaptcha 组件
        self.assertEqual(page.evaluate.await_count, 1)

    async def test_solve_captcha_and_submit_gives_up_after_attempts(self) -> None:
        page, _, _ = self._register_page([500, 500, 500])
        solver = Mock()
        solver.solve = AsyncMock(return_value=True)
        config = _app_config("llm")

        with patch("main.asyncio.sleep", new=AsyncMock()):
            ok = await _solve_captcha_and_submit(page, solver, config)

        self.assertFalse(ok)
        self.assertEqual(solver.solve.await_count, 3)

    async def test_request_new_verification_code_clicks_resend_link(self) -> None:
        link = Mock()
        link.count = AsyncMock(return_value=1)
        link.click = AsyncMock()
        link.filter = Mock(return_value=link)
        link.first = link

        page = Mock()
        page.get_by_text.return_value = link

        with patch("main.asyncio.sleep", new=AsyncMock()):
            clicked = await _request_new_verification_code(page)

        self.assertTrue(clicked)
        link.click.assert_awaited_once()

    async def test_recover_from_navigation_error_reloads(self) -> None:
        page = Mock(url="https://build.nvidia.com/")
        page.reload = AsyncMock()
        page.goto = AsyncMock()

        recovered = await _recover_from_navigation_error(page)

        self.assertTrue(recovered)
        page.reload.assert_awaited_once()
        page.goto.assert_not_awaited()

    async def test_recover_from_navigation_error_falls_back_to_goto(self) -> None:
        page = Mock(url="chrome-error://chromewebdata/")
        page.reload = AsyncMock(side_effect=Exception("boom"))

        async def fake_goto(*_args, **_kwargs) -> None:
            page.url = "https://build.nvidia.com/"

        page.goto = AsyncMock(side_effect=fake_goto)

        recovered = await _recover_from_navigation_error(page)

        self.assertTrue(recovered)
        page.goto.assert_awaited_once()


def _app_config(captcha_mode: str, concurrency: int = 1) -> AppConfig:
    captcha = CaptchaConfig(
        mode=captcha_mode,
        yescaptcha_client_key=None,
        yescaptcha_api_url="https://example.test",
        captcharun_token=None,
        captcharun_api_url="https://example.test",
        llm_model="vision-model" if captcha_mode == "llm" else None,
        llm_api_base="https://example.test/v1",
        llm_api_key="secret" if captcha_mode == "llm" else None,
        llm_api_protocol="responses",
        llm_reasoning_effort=None,
        llm_call_delay_seconds=0,
        llm_action_delay_seconds=0,
        llm_calls_per_attempt=1,
        llm_max_attempts=1,
        llm_max_output_tokens=128,
        llm_max_concurrency=1,
        llm_artifact_dir=None,
        poll_interval_seconds=1,
        timeout_seconds=1,
    )
    return AppConfig(
        email_provider="duckmail",
        cloudflare_temp_email=CloudflareTempEmailConfig("", "", ""),
        duckmail=DuckMailConfig("https://example.test", "example.test", None),
        captcha=captcha,
        nvidia=NvidiaConfig(
            output_csv=Path("accounts.csv"),
            key_name="api",
            account_name="NVIDIA Build",
            key_expiry_date="2126-05-08T08:00:00Z",
        ),
        browser=BrowserConfig(
            headless=True,
            concurrency=concurrency,
            launch_stagger_seconds=0,
            close_delay_seconds=0,
        ),
    )
