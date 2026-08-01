from __future__ import annotations

import base64
import unittest
from typing import Any, cast
from unittest.mock import AsyncMock, Mock, patch

from captcha import (
    CaptchaRunSolver,
    LLMCaptchaAction,
    LLMCaptchaDecision,
    LLMCaptchaSolver,
    ManualCaptchaSolver,
    YesCaptchaSolver,
    _capture_llm_captcha,
    _apply_llm_action,
    _captcha_action_error,
    _click_hcaptcha_checkbox,
    _click_hcaptcha_submit,
    _llm_captcha_prompt,
    _parse_llm_decision,
    _parse_pattern_grounding,
    _responses_endpoint,
    build_captcha_solver,
)
from config import CaptchaConfig
from playwright.async_api import Page


class FakeMouse:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def click(self, x: float, y: float, delay: int) -> None:
        self.calls.append(("click", x, y, delay))

    async def move(self, x: float, y: float, steps: int = 1) -> None:
        self.calls.append(("move", x, y, steps))

    async def down(self) -> None:
        self.calls.append(("down",))

    async def up(self) -> None:
        self.calls.append(("up",))


class FakePage:
    viewport_size = {"width": 1280, "height": 800}

    def __init__(self) -> None:
        self.mouse = FakeMouse()
        self.frames: list[Any] = []
        self.screenshot_count = 0

    async def screenshot(self, type: str, clip=None) -> bytes:
        self.screenshot_count += 1
        self.last_clip = clip
        return f"screenshot-{self.screenshot_count}-{type}".encode()

    async def evaluate(self, expression: str):
        return False


class LLMCaptchaTests(unittest.IsolatedAsyncioTestCase):
    def _config(self, mode: str) -> CaptchaConfig:
        return CaptchaConfig(
            mode=mode,
            yescaptcha_client_key="yes-key",
            yescaptcha_api_url="https://yes.example.test",
            captcharun_token="run-token",
            captcharun_api_url="https://run.example.test",
            llm_model="vision-model",
            llm_api_base="https://llm.example.test/v1",
            llm_api_key="llm-key",
            llm_reasoning_effort=None,
            llm_call_delay_seconds=5,
            llm_action_delay_seconds=5,
            llm_calls_per_attempt=10,
            llm_max_attempts=2,
            llm_max_output_tokens=1200,
            llm_artifact_dir=None,
            poll_interval_seconds=3,
            timeout_seconds=180,
        )

    def test_build_solver_keeps_all_modes_available(self) -> None:
        expected = {
            "manual": ManualCaptchaSolver,
            "yescaptcha": YesCaptchaSolver,
            "captcharun": CaptchaRunSolver,
            "llm": LLMCaptchaSolver,
        }

        for mode, solver_type in expected.items():
            with self.subTest(mode=mode):
                self.assertIsInstance(build_captcha_solver(self._config(mode)), solver_type)

    def test_prompt_requires_one_action_from_fresh_screenshot(self) -> None:
        prompt = " ".join(
            _llm_captcha_prompt(
                1280,
                800,
                1,
                2,
                "Drag ONE animal to the matching silhouette",
                "Check",
                "hcaptcha_canvas",
            ).split()
        )

        self.assertIn("at most ONE action", prompt)
        self.assertIn("send a fresh screenshot", prompt)
        self.assertIn("green checked hCaptcha checkbox", prompt)
        self.assertIn("outer-page submit controls", prompt)
        self.assertIn("native backing resolution", prompt)
        self.assertIn("Drag ONE animal", prompt)
        self.assertIn('status "verify"', prompt)

    def test_parser_accepts_dom_verify_request(self) -> None:
        decision = _parse_llm_decision(
            '{"status":"verify","actions":[],"message":"selection complete"}',
            1000,
            940,
        )

        self.assertEqual(decision.status, "verify")
        self.assertEqual(decision.actions, ())

    def test_rejects_action_kind_that_conflicts_with_instruction(self) -> None:
        decision = LLMCaptchaDecision(
            "actions",
            (LLMCaptchaAction("drag", 100, 100, 200, 200),),
            "",
        )

        error = _captcha_action_error(decision, "Click the two arrows")

        self.assertIn("requires click", error or "")

    def test_parses_pixel_grounded_pattern_action(self) -> None:
        action = _parse_pattern_grounding(
            {
                "sourceIndex": 2,
                "targetRow": 4,
                "targetColumn": 3,
                "startX": 140,
                "startY": 536,
                "endX": 700,
                "endY": 827,
            },
            1000,
            940,
        )

        self.assertEqual(action, LLMCaptchaAction("drag", 140, 536, 700, 827))

    def test_parser_keeps_only_first_action_for_fresh_feedback(self) -> None:
        decision = _parse_llm_decision(
            """{
                "status": "actions",
                "actions": [
                    {"kind": "click", "start_x": 120, "start_y": 80,
                     "end_x": null, "end_y": null},
                    {"kind": "drag", "start_x": 200, "start_y": 220,
                     "end_x": 420, "end_y": 260}
                ],
                "message": "act in order"
            }""",
            1280,
            800,
        )

        self.assertEqual([action.kind for action in decision.actions], ["click"])
        self.assertEqual(decision.ignored_action_count, 1)

    def test_parser_extracts_json_from_compatibility_wrapper(self) -> None:
        decision = _parse_llm_decision(
            """I inspected the image.\n```json
            {"status":"actions","actions":[],"message":"wait"}
            ```""",
            1280,
            800,
        )

        self.assertEqual(decision.status, "actions")
        self.assertEqual(decision.actions, ())

    def test_rejects_action_outside_viewport(self) -> None:
        with self.assertRaisesRegex(ValueError, "outside the viewport"):
            _parse_llm_decision(
                """{
                    "status": "actions",
                    "actions": [{"kind": "click", "start_x": 1281, "start_y": 80,
                                 "end_x": null, "end_y": null}],
                    "message": ""
                }""",
                1280,
                800,
            )

    def test_converts_normalized_grounding_coordinates(self) -> None:
        decision = _parse_llm_decision(
            """{
                "status": "actions",
                "actions": [{"kind": "drag", "start_x": 803, "start_y": 306,
                             "end_x": 477, "end_y": 392}],
                "message": "drag the rooster"
            }""",
            536,
            587,
        )

        self.assertEqual(decision.coordinate_space, "normalized_1000")
        self.assertEqual(decision.actions[0].start_x, 430.41)
        self.assertEqual(decision.actions[0].start_y, 179.62)
        self.assertEqual(decision.actions[0].end_x, 255.67)
        self.assertEqual(decision.actions[0].end_y, 230.1)

    def test_explicit_normalized_space_converts_in_bounds_values(self) -> None:
        decision = _parse_llm_decision(
            """{
                "status": "actions",
                "actions": [{"kind": "click", "start_x": 186, "start_y": 341,
                             "end_x": 329, "end_y": 467}],
                "message": "top left",
                "coordinate_space": "normalized_1000"
            }""",
            416,
            617,
        )

        self.assertEqual(decision.coordinate_space, "normalized_1000")
        self.assertEqual(decision.actions[0].start_x, 77.38)
        self.assertEqual(decision.actions[0].start_y, 210.4)
        self.assertIsNone(decision.actions[0].end_x)

    def test_responses_endpoint_accepts_base_or_full_url(self) -> None:
        self.assertEqual(
            _responses_endpoint("https://api.example.test/v1"),
            "https://api.example.test/v1/responses",
        )
        self.assertEqual(
            _responses_endpoint("https://api.example.test/v1/responses"),
            "https://api.example.test/v1/responses",
        )

    def test_request_uses_image_and_strict_json_schema(self) -> None:
        response = Mock(ok=True, status_code=200)
        response.json.return_value = {
            "id": "response-1",
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": '{"status":"solved","actions":[],"message":"done"}',
                        }
                    ],
                }
            ],
        }
        solver = LLMCaptchaSolver(
            "vision-model",
            "https://api.example.test/v1",
            "key",
            30,
            reasoning_effort="high",
            max_output_tokens=2048,
        )

        with patch("captcha.requests.post", return_value=response) as post:
            decision = solver._request_decision(b"png", 1280, 800, 1, 1)

        self.assertEqual(decision.status, "solved")
        self.assertEqual(decision.response_id, "response-1")
        request = post.call_args
        self.assertEqual(request.args[0], "https://api.example.test/v1/responses")
        body = request.kwargs["json"]
        self.assertEqual(body["model"], "vision-model")
        self.assertEqual(body["reasoning"], {"effort": "high"})
        self.assertEqual(body["max_output_tokens"], 2048)
        self.assertTrue(body["text"]["format"]["strict"])
        self.assertEqual(body["text"]["format"]["type"], "json_schema")
        self.assertEqual(body["text"]["format"]["schema"]["properties"]["actions"]["maxItems"], 1)
        self.assertIn(
            "coordinate_space",
            body["text"]["format"]["schema"]["required"],
        )
        self.assertTrue(
            body["input"][0]["content"][1]["image_url"].startswith(
                "data:image/png;base64,"
            )
        )

    async def test_solver_recaptures_between_click_and_drag(self) -> None:
        page = FakePage()
        click_decision = LLMCaptchaDecision(
            "actions",
            (LLMCaptchaAction("click", 100, 120, None, None),),
            "",
        )
        drag_decision = LLMCaptchaDecision(
            "actions",
            (LLMCaptchaAction("drag", 200, 220, 360, 260),),
            "",
        )
        solver = LLMCaptchaSolver(
            "vision-model",
            "https://api.example.test/v1",
            "key",
            30,
            call_delay_seconds=0,
            action_delay_seconds=0,
            calls_per_attempt=2,
            max_attempts=1,
        )

        with (
            patch.object(
                LLMCaptchaSolver,
                "_request_decision",
                side_effect=[click_decision, drag_decision],
            ),
            patch("captcha.asyncio.sleep", new=AsyncMock()),
            patch(
                "captcha._click_hcaptcha_checkbox",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "captcha._is_captcha_target_enabled",
                new=AsyncMock(side_effect=[False, False, False, True]),
            ),
        ):
            solved = await solver.solve(cast(Page, page))

        self.assertTrue(solved)
        self.assertEqual(page.mouse.calls[0], ("click", 100, 120, 80))
        self.assertEqual(page.mouse.calls[1], ("move", 200, 220, 1))
        self.assertEqual(page.mouse.calls[2], ("down",))
        self.assertEqual(page.mouse.calls[4], ("up",))
        self.assertEqual(page.screenshot_count, 2)

    async def test_click_checkbox_waits_for_hcaptcha_frame(self) -> None:
        checkbox = Mock()
        checkbox.count = AsyncMock(return_value=1)
        checkbox.is_visible = AsyncMock(return_value=True)
        checkbox.get_attribute = AsyncMock(return_value="false")
        checkbox.click = AsyncMock()
        frame = Mock(url="https://newassets.hcaptcha.com/captcha/v1/checkbox")
        frame.locator.return_value = checkbox
        page = Mock(frames=[frame])

        clicked = await _click_hcaptcha_checkbox(cast(Page, page), timeout_seconds=1)

        self.assertTrue(clicked)
        checkbox.click.assert_awaited_once_with(delay=80, timeout=5000)

    async def test_clicks_non_skip_hcaptcha_submit_control(self) -> None:
        button = Mock()
        button.count = AsyncMock(return_value=1)
        button.is_visible = AsyncMock(return_value=True)
        button.evaluate = AsyncMock(return_value="检查 submit answer")
        button.get_attribute = AsyncMock(return_value=None)
        button.click = AsyncMock()
        locator = Mock()
        locator.first = button
        frame = Mock(
            url="https://newassets.hcaptcha.com/captcha/static/hcaptcha.html#frame=challenge"
        )
        frame.locator.return_value = locator
        page = Mock(frames=[frame])

        clicked = await _click_hcaptcha_submit(cast(Page, page), timeout_seconds=0)

        self.assertTrue(clicked)
        button.click.assert_awaited_once_with(delay=80, timeout=5000)

    async def test_action_maps_native_canvas_coordinates_to_css_page(self) -> None:
        page = FakePage()

        await _apply_llm_action(
            cast(Page, page),
            LLMCaptchaAction("click", 600, 400, None, None),
            offset_x=100,
            offset_y=50,
            scale_x=0.5,
            scale_y=0.5,
        )

        self.assertEqual(page.mouse.calls, [("click", 400, 250, 80)])

    async def test_capture_prefers_native_hcaptcha_canvas(self) -> None:
        png = b"\x89PNG\r\n\x1a\nnative"
        canvas = Mock()
        canvas.count = AsyncMock(return_value=1)
        canvas.is_visible = AsyncMock(return_value=True)
        canvas.evaluate = AsyncMock(
            return_value={
                "dataUrl": "data:image/png;base64," + base64.b64encode(png).decode(),
                "width": 1000,
                "height": 940,
                "cssX": 10,
                "cssY": 10,
                "cssWidth": 500,
                "cssHeight": 470,
                "prompt": "Drag ONE animal to the matching silhouette",
                "submitLabel": "跳过 skip challenge",
                "patternGrounding": {
                    "sourceIndex": 1,
                    "targetRow": 3,
                    "targetColumn": 1,
                    "startX": 140,
                    "startY": 357,
                    "endX": 405,
                    "endY": 675,
                    "rowRatio": 3.2,
                    "cellRatio": 2.0,
                    "candidateRatio": 1.8,
                },
            }
        )
        locator = Mock()
        locator.first = canvas
        frame = Mock(
            url="https://newassets.hcaptcha.com/captcha/static/hcaptcha.html#frame=challenge"
        )
        frame.locator.return_value = locator
        page = FakePage()
        page.frames = [frame]
        page.evaluate = AsyncMock(
            return_value={"x": 300, "y": 100, "width": 500, "height": 560}
        )

        capture = await _capture_llm_captcha(cast(Page, page))

        self.assertEqual(capture.source, "hcaptcha_canvas")
        self.assertEqual(capture.content, png)
        self.assertEqual((capture.width, capture.height), (1000, 940))
        self.assertEqual((capture.offset_x, capture.offset_y), (310, 110))
        self.assertEqual((capture.scale_x, capture.scale_y), (0.5, 0.5))
        self.assertEqual(
            capture.challenge_prompt,
            "Drag ONE animal to the matching silhouette",
        )
        self.assertEqual(
            capture.grounded_action,
            LLMCaptchaAction("drag", 140, 357, 405, 675),
        )

    async def test_capture_crops_visible_hcaptcha_iframe(self) -> None:
        page = FakePage()
        page.evaluate = AsyncMock(
            return_value={"x": 300, "y": 100, "width": 500, "height": 600}
        )

        capture = await _capture_llm_captcha(cast(Page, page))

        self.assertEqual(capture.source, "hcaptcha_iframe")
        self.assertEqual((capture.width, capture.height), (500, 600))
        self.assertEqual((capture.offset_x, capture.offset_y), (300, 100))
        self.assertEqual(
            page.last_clip,
            {"x": 300.0, "y": 100.0, "width": 500.0, "height": 600.0},
        )


if __name__ == "__main__":
    unittest.main()
