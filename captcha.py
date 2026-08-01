from __future__ import annotations

import asyncio
import base64
import json
import math
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import urlparse, parse_qs
from uuid import uuid4

import requests
from playwright.async_api import Page

from config import CaptchaConfig


class CaptchaSolver(Protocol):
    async def solve(self, page: Page) -> bool:
        ...


class ManualCaptchaSolver:
    async def solve(self, page: Page) -> bool:
        print("\n[2/4] Please solve the hCaptcha manually...")
        for i in range(120):
            if await _is_register_button_enabled(page):
                print(f"  hCaptcha solved ({i}s)")
                return True
            await asyncio.sleep(1)
        print("  hCaptcha timeout")
        return False


LLM_CALLS_PER_ATTEMPT = 10
LLM_MAX_ATTEMPTS = 2
LLM_MAX_ACTIONS_PER_CALL = 1
LLM_CALL_DELAY_SECONDS = 5
LLM_ACTION_DELAY_SECONDS = 5

LLM_ACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["status", "actions", "message", "coordinate_space"],
    "properties": {
        "status": {"type": "string", "enum": ["actions", "verify", "solved"]},
        "actions": {
            "type": "array",
            "maxItems": LLM_MAX_ACTIONS_PER_CALL,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["kind", "start_x", "start_y", "end_x", "end_y"],
                "properties": {
                    "kind": {"type": "string", "enum": ["click", "drag"]},
                    "start_x": {"type": "number"},
                    "start_y": {"type": "number"},
                    "end_x": {"type": ["number", "null"]},
                    "end_y": {"type": ["number", "null"]},
                },
            },
        },
        "message": {"type": "string"},
        "coordinate_space": {
            "type": "string",
            "enum": ["normalized_1000"],
        },
    },
}


@dataclass(frozen=True)
class LLMCaptchaAction:
    kind: str
    start_x: float
    start_y: float
    end_x: float | None
    end_y: float | None


@dataclass(frozen=True)
class LLMCaptchaDecision:
    status: str
    actions: tuple[LLMCaptchaAction, ...]
    message: str
    response_id: str | None = None
    usage: dict[str, Any] | None = None
    raw_output: str = ""
    ignored_action_count: int = 0
    coordinate_space: str = "pixels"


@dataclass(frozen=True)
class LLMCaptchaCapture:
    content: bytes
    width: int
    height: int
    offset_x: float = 0
    offset_y: float = 0
    scale_x: float = 1
    scale_y: float = 1
    source: str = "viewport"
    challenge_prompt: str = ""
    submit_label: str = ""
    grounded_action: LLMCaptchaAction | None = None
    grounding: dict[str, Any] | None = None


class LLMOutputError(ValueError):
    def __init__(self, message: str, raw_output: str):
        super().__init__(message)
        self.raw_output = raw_output


@dataclass
class LLMCaptchaSolver:
    model: str
    api_base: str
    api_key: str
    timeout_seconds: int
    reasoning_effort: str | None = None
    call_delay_seconds: int = LLM_CALL_DELAY_SECONDS
    action_delay_seconds: int = LLM_ACTION_DELAY_SECONDS
    calls_per_attempt: int = LLM_CALLS_PER_ATTEMPT
    max_attempts: int = LLM_MAX_ATTEMPTS
    max_output_tokens: int = 1200
    artifact_dir: Path | None = None
    last_error: str | None = None
    _active_artifact_dir: Path | None = field(default=None, init=False, repr=False)

    def _start_artifact_session(self) -> None:
        self._active_artifact_dir = None
        if not self.artifact_dir:
            return
        session_name = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:8]}"
        session_dir = self.artifact_dir / session_name
        session_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
        self._active_artifact_dir = session_dir
        print(f"  LLM captcha artifacts: {session_dir}")

    def _save_capture(self, filename: str, content: bytes) -> str | None:
        if not self._active_artifact_dir:
            return None
        path = self._active_artifact_dir / filename
        path.write_bytes(content)
        path.chmod(0o600)
        return filename

    def _trace(self, event: str, **details: Any) -> None:
        if not self._active_artifact_dir:
            return
        path = self._active_artifact_dir / "trace.jsonl"
        entry = {"event": event, "time": time.time(), **details}
        try:
            with path.open("a", encoding="utf-8") as file:
                file.write(json.dumps(entry, ensure_ascii=False) + "\n")
            path.chmod(0o600)
        except OSError as exc:
            print(f"  LLM artifact write failed: {exc}")

    async def solve(self, page: Page) -> bool:
        print("\n[2/4] Solving visual captcha with LLM Responses API...")
        self.last_error = None
        self._start_artifact_session()
        deadline = time.time() + self.timeout_seconds
        total_calls = 0
        last_detail = ""
        next_delay = self.call_delay_seconds

        for attempt in range(1, self.max_attempts + 1):
            print(f"  LLM captcha attempt {attempt}/{self.max_attempts}")
            self._trace("attempt_start", attempt=attempt)
            if await _click_hcaptcha_checkbox(page):
                print("  hCaptcha checkbox clicked directly; waiting for challenge")
                self._trace("checkbox_click", attempt=attempt)
                next_delay = self.action_delay_seconds
            for call_number in range(1, self.calls_per_attempt + 1):
                await asyncio.sleep(next_delay)
                next_delay = self.call_delay_seconds

                if time.time() >= deadline:
                    print("  LLM captcha timeout")
                    self.last_error = f"LLM captcha timed out after {total_calls} calls"
                    self._trace("timeout", calls=total_calls, error=self.last_error)
                    return False
                if await _is_captcha_target_enabled(page):
                    print("  captcha target is enabled")
                    self._trace("solved", attempt=attempt, call=call_number)
                    return True

                capture = await _capture_llm_captcha(page)
                screenshot_name = self._save_capture(
                    f"attempt-{attempt:02d}-call-{call_number:02d}.png",
                    capture.content,
                )
                total_calls += 1
                capture_details = {
                    "source": capture.source,
                    "width": capture.width,
                    "height": capture.height,
                    "offset_x": capture.offset_x,
                    "offset_y": capture.offset_y,
                    "scale_x": capture.scale_x,
                    "scale_y": capture.scale_y,
                    "challenge_prompt": capture.challenge_prompt,
                    "submit_label": capture.submit_label,
                    "grounding": capture.grounding,
                }
                self._trace(
                    "request",
                    attempt=attempt,
                    call=call_number,
                    screenshot=screenshot_name,
                    capture=capture_details,
                )
                grounded = capture.grounded_action is not None
                if capture.grounded_action:
                    decision = LLMCaptchaDecision(
                        "actions",
                        (capture.grounded_action,),
                        "pixel-grounded animal pattern",
                        coordinate_space="pixels",
                    )
                else:
                    try:
                        decision = await asyncio.to_thread(
                            self._request_decision,
                            capture.content,
                            capture.width,
                            capture.height,
                            attempt,
                            call_number,
                            capture.challenge_prompt,
                            capture.submit_label,
                            capture.source,
                        )
                    except Exception as exc:
                        last_detail = str(exc)
                        print(
                            f"  LLM call {call_number}/{self.calls_per_attempt} failed: {exc}"
                        )
                        raw_output = getattr(exc, "raw_output", "")
                        if raw_output:
                            print(f"    raw output: {_output_preview(raw_output)}")
                        self._trace(
                            "response_error",
                            attempt=attempt,
                            call=call_number,
                            screenshot=screenshot_name,
                            capture=capture_details,
                            error=str(exc),
                            raw_output=raw_output,
                        )
                        continue

                action_error = _captcha_action_error(decision, capture.challenge_prompt)
                if action_error:
                    last_detail = action_error
                    print(f"  LLM call {call_number}/{self.calls_per_attempt} rejected: {action_error}")
                    self._trace(
                        "response_error",
                        attempt=attempt,
                        call=call_number,
                        screenshot=screenshot_name,
                        capture=capture_details,
                        error=action_error,
                        raw_output=decision.raw_output,
                    )
                    continue

                last_detail = decision.message or decision.status
                source_label = "grounded pattern" if grounded else "LLM call"
                print(
                    f"  {source_label} {call_number}/{self.calls_per_attempt}: "
                    f"{decision.status}, {len(decision.actions)} action(s)"
                )
                if decision.ignored_action_count:
                    print(
                        f"    ignored {decision.ignored_action_count} stale action(s); "
                        "a fresh screenshot will be used"
                    )
                if decision.coordinate_space != "pixels":
                    print(f"    converted {decision.coordinate_space} coordinates to pixels")
                self._trace(
                    "response",
                    attempt=attempt,
                    call=call_number,
                    screenshot=screenshot_name,
                    capture=capture_details,
                    status=decision.status,
                    message=decision.message,
                    actions=[
                        {
                            "kind": action.kind,
                            "start_x": action.start_x,
                            "start_y": action.start_y,
                            "end_x": action.end_x,
                            "end_y": action.end_y,
                        }
                        for action in decision.actions
                    ],
                    ignored_action_count=decision.ignored_action_count,
                    coordinate_space=decision.coordinate_space,
                    response_id=decision.response_id,
                    usage=decision.usage,
                    raw_output=decision.raw_output,
                )
                if decision.status == "failed":
                    print("    model returned no action; retrying with a fresh call")
                    continue

                if decision.status == "verify":
                    submitted = await _click_hcaptcha_submit(page)
                    print(f"    hCaptcha submit requested (clicked={submitted})")
                    self._trace(
                        "captcha_submit",
                        attempt=attempt,
                        call=call_number,
                        clicked=submitted,
                    )
                    next_delay = self.action_delay_seconds
                    continue

                if decision.actions:
                    action = decision.actions[0]
                    await _apply_llm_action(
                        page,
                        action,
                        offset_x=capture.offset_x,
                        offset_y=capture.offset_y,
                        scale_x=capture.scale_x,
                        scale_y=capture.scale_y,
                    )
                    self._trace(
                        "action",
                        attempt=attempt,
                        call=call_number,
                        action={
                            "kind": action.kind,
                            "start_x": (
                                action.start_x * capture.scale_x + capture.offset_x
                            ),
                            "start_y": (
                                action.start_y * capture.scale_y + capture.offset_y
                            ),
                            "end_x": (
                                action.end_x * capture.scale_x + capture.offset_x
                                if action.end_x is not None
                                else None
                            ),
                            "end_y": (
                                action.end_y * capture.scale_y + capture.offset_y
                                if action.end_y is not None
                                else None
                            ),
                        },
                    )
                    if await _is_captcha_target_enabled(page):
                        print("  captcha solved by LLM actions")
                        self._trace("solved", attempt=attempt, call=call_number)
                        return True
                    if action.kind == "drag":
                        submitted = await _click_hcaptcha_submit(page)
                        print(f"    drag complete; hCaptcha submit clicked={submitted}")
                        self._trace(
                            "captcha_submit",
                            attempt=attempt,
                            call=call_number,
                            clicked=submitted,
                            after_action="drag",
                        )
                    next_delay = self.action_delay_seconds
                    continue

                if await _is_captcha_target_enabled(page):
                    print("  captcha solved by LLM")
                    self._trace("solved", attempt=attempt, call=call_number)
                    return True
                if decision.status == "solved":
                    print("    model reported solved; waiting for browser confirmation")

            if attempt < self.max_attempts:
                reset = await _reset_hcaptcha(page)
                print(f"  starting next LLM captcha attempt (reset={reset})")
                self._trace("reset", attempt=attempt, reset=reset)
                next_delay = self.call_delay_seconds

        suffix = f"; last result: {last_detail}" if last_detail else ""
        self.last_error = (
            f"LLM captcha failed after {self.max_attempts} attempts and "
            f"{total_calls} calls{suffix}"
        )
        print(f"  {self.last_error}")
        try:
            final_capture = await _capture_llm_captcha(page)
            final_name = self._save_capture("final.png", final_capture.content)
        except Exception:
            final_name = None
        self._trace("failed", calls=total_calls, error=self.last_error, screenshot=final_name)
        return False

    def _request_decision(
        self,
        screenshot: bytes,
        width: int,
        height: int,
        attempt: int,
        call_number: int,
        challenge_prompt: str = "",
        submit_label: str = "",
        capture_source: str = "viewport",
    ) -> LLMCaptchaDecision:
        image_data = base64.b64encode(screenshot).decode("ascii")
        request_payload: dict[str, Any] = {
            "model": self.model,
            "input": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": _llm_captcha_prompt(
                                width,
                                height,
                                attempt,
                                call_number,
                                challenge_prompt,
                                submit_label,
                                capture_source,
                            ),
                        },
                        {
                            "type": "input_image",
                            "image_url": f"data:image/png;base64,{image_data}",
                            "detail": "high",
                        },
                    ],
                }
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "captcha_actions",
                    "strict": True,
                    "schema": LLM_ACTION_SCHEMA,
                }
            },
            "max_output_tokens": self.max_output_tokens,
        }
        if self.reasoning_effort:
            request_payload["reasoning"] = {"effort": self.reasoning_effort}
        response = requests.post(
            _responses_endpoint(self.api_base),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json=request_payload,
            timeout=min(90, max(10, self.timeout_seconds)),
        )
        if not response.ok:
            detail = response.text[:300].replace("\n", " ")
            raise RuntimeError(f"Responses API HTTP {response.status_code}: {detail}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError("Responses API returned invalid JSON") from exc
        output_text = _responses_output_text(payload)
        try:
            decision = _parse_llm_decision(output_text, width, height)
        except ValueError as exc:
            raise LLMOutputError(str(exc), output_text) from exc
        return replace(
            decision,
            response_id=str(payload["id"]) if payload.get("id") else None,
            usage=payload.get("usage") if isinstance(payload.get("usage"), dict) else None,
            raw_output=output_text,
        )


@dataclass(frozen=True)
class YesCaptchaSolver:
    client_key: str
    api_url: str
    poll_interval_seconds: int
    timeout_seconds: int

    async def solve(self, page: Page) -> bool:
        print("\n[2/4] Solving hCaptcha with YesCaptcha...")
        site_key = await _get_site_key(page)
        if not site_key:
            print("  hCaptcha sitekey not found")
            return False

        task_id = self._create_task(page.url, site_key)
        token = self._poll_task_result(task_id)
        if not token:
            return False

        await _inject_hcaptcha_token(page, token)
        for i in range(20):
            if await _is_register_button_enabled(page):
                print(f"  hCaptcha solved by YesCaptcha ({i}s)")
                return True
            await asyncio.sleep(1)
        print("  hCaptcha token injected, but #register_button stayed disabled")
        return False

    def _create_task(self, website_url: str, website_key: str) -> str:
        response = requests.post(
            f"{self.api_url}/createTask",
            json={
                "clientKey": self.client_key,
                "task": {
                    "type": "HCaptchaTaskProxyless",
                    "websiteURL": website_url,
                    "websiteKey": website_key,
                },
            },
            timeout=30,
        )
        data = response.json()
        if data.get("errorId"):
            raise RuntimeError(f"YesCaptcha createTask failed: {data}")
        task_id = data.get("taskId")
        if not task_id:
            raise RuntimeError(f"YesCaptcha createTask missing taskId: {data}")
        return str(task_id)

    def _poll_task_result(self, task_id: str) -> str | None:
        deadline = time.time() + self.timeout_seconds
        while time.time() < deadline:
            response = requests.post(
                f"{self.api_url}/getTaskResult",
                json={"clientKey": self.client_key, "taskId": task_id},
                timeout=30,
            )
            data = response.json()
            if data.get("errorId"):
                print(f"  YesCaptcha getTaskResult failed: {data}")
                return None
            if data.get("status") == "ready":
                solution = data.get("solution") or {}
                return solution.get("gRecaptchaResponse") or solution.get("token")
            time.sleep(self.poll_interval_seconds)
        print("  YesCaptcha timeout")
        return None


@dataclass(frozen=True)
class CaptchaRunSolver:
    token: str
    api_url: str
    poll_interval_seconds: int
    timeout_seconds: int

    async def solve(self, page: Page) -> bool:
        print("\n[2/4] Solving hCaptcha with CaptchaRun...")
        site_key = await _get_site_key(page)
        if not site_key:
            print("  hCaptcha sitekey not found")
            return False

        user_agent = await page.evaluate("() => navigator.userAgent")
        task_id, token = self._create_task(page.url, site_key, user_agent)
        if task_id and not token:
            token = self._poll_task_result(task_id)
        if not token:
            return False

        await _inject_hcaptcha_token(page, token)
        for i in range(20):
            if await _is_register_button_enabled(page):
                print(f"  hCaptcha solved by CaptchaRun ({i}s)")
                return True
            await asyncio.sleep(1)
        print("  hCaptcha token injected, but #register_button stayed disabled")
        return False

    def _create_task(self, website_url: str, website_key: str, user_agent: str) -> tuple[str | None, str | None]:
        response = requests.post(
            f"{self.api_url}/v2/tasks",
            headers=self._headers(),
            json={
                "captchaType": "HCaptcha",
                "siteKey": website_key,
                "siteReferer": _site_referer(website_url),
                "userAgent": user_agent,
                "fallbackToActualUA": True,
            },
            timeout=30,
        )
        data = _response_json(response)
        if not response.ok:
            raise RuntimeError(f"CaptchaRun create task failed: {data}")
        task_id = data.get("taskId")
        result = data.get("result") or {}
        token = _extract_hcaptcha_token(result)
        if not task_id and not token:
            raise RuntimeError(f"CaptchaRun create task missing taskId/result: {data}")
        return str(task_id) if task_id else None, token

    def _poll_task_result(self, task_id: str) -> str | None:
        deadline = time.time() + self.timeout_seconds
        while time.time() < deadline:
            response = requests.get(
                f"{self.api_url}/v2/tasks/{task_id}",
                headers=self._headers(content_type=False),
                timeout=30,
            )
            data = _response_json(response)
            if not response.ok:
                print(f"  CaptchaRun get task result failed: {data}")
                return None

            status = str(data.get("status", "")).lower()
            if status == "success":
                return _extract_hcaptcha_token(data.get("response") or data.get("result") or {})
            if status == "fail":
                print(f"  CaptchaRun failed: {data.get('reason') or data}")
                return None
            time.sleep(self.poll_interval_seconds)
        print("  CaptchaRun timeout")
        return None

    def _headers(self, content_type: bool = True) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self.token}"}
        if content_type:
            headers["Content-Type"] = "application/json"
        return headers


async def _click_hcaptcha_checkbox(page: Page, timeout_seconds: int = 15) -> bool:
    poll_seconds = 0.5
    attempts = max(1, round(timeout_seconds / poll_seconds))
    for attempt in range(attempts):
        for frame in getattr(page, "frames", []):
            if "hcaptcha" not in frame.url.lower():
                continue
            for selector in ("#checkbox", '[role="checkbox"]'):
                checkbox = frame.locator(selector)
                try:
                    if await checkbox.count() < 1 or not await checkbox.is_visible():
                        continue
                    if await checkbox.get_attribute("aria-checked") == "true":
                        return False
                    await checkbox.click(delay=80, timeout=5000)
                    return True
                except Exception:
                    continue
        if attempt < attempts - 1:
            await asyncio.sleep(poll_seconds)
    return False


async def _capture_llm_captcha(page: Page) -> LLMCaptchaCapture:
    native_capture = await _capture_native_hcaptcha_canvas(page)
    if native_capture:
        return native_capture

    viewport = page.viewport_size or await page.evaluate(
        "() => ({width: window.innerWidth, height: window.innerHeight})"
    )
    viewport_width = int(viewport["width"])
    viewport_height = int(viewport["height"])
    rect = await page.evaluate(
        """() => {
            const viewportWidth = window.innerWidth;
            const viewportHeight = window.innerHeight;
            const candidates = Array.from(document.querySelectorAll('iframe'))
                .map((iframe) => {
                    const marker = [iframe.src, iframe.title, iframe.name]
                        .filter(Boolean).join(' ').toLowerCase();
                    if (!marker.includes('hcaptcha')) return null;
                    const style = getComputedStyle(iframe);
                    const box = iframe.getBoundingClientRect();
                    if (style.display === 'none' || style.visibility === 'hidden' ||
                        Number(style.opacity || 1) === 0 || box.width < 20 || box.height < 20) {
                        return null;
                    }
                    const left = Math.max(0, box.left);
                    const top = Math.max(0, box.top);
                    const right = Math.min(viewportWidth, box.right);
                    const bottom = Math.min(viewportHeight, box.bottom);
                    if (right <= left || bottom <= top) return null;
                    return {left, top, right, bottom, area: (right - left) * (bottom - top)};
                })
                .filter(Boolean)
                .sort((a, b) => b.area - a.area);
            if (!candidates.length) return null;
            const box = candidates[0];
            const padding = 8;
            const x = Math.max(0, Math.floor(box.left) - padding);
            const y = Math.max(0, Math.floor(box.top) - padding);
            const right = Math.min(viewportWidth, Math.ceil(box.right) + padding);
            const bottom = Math.min(viewportHeight, Math.ceil(box.bottom) + padding);
            return {x, y, width: right - x, height: bottom - y};
        }"""
    )
    if isinstance(rect, dict):
        values = [rect.get(key) for key in ("x", "y", "width", "height")]
        if all(
            not isinstance(value, bool) and isinstance(value, (int, float))
            for value in values
        ):
            x, y, width, height = (
                float(cast(int | float, value)) for value in values
            )
            if width >= 20 and height >= 20:
                content = await page.screenshot(
                    type="png",
                    clip={"x": x, "y": y, "width": width, "height": height},
                )
                return LLMCaptchaCapture(
                    content=content,
                    width=round(width),
                    height=round(height),
                    offset_x=x,
                    offset_y=y,
                    source="hcaptcha_iframe",
                )

    content = await page.screenshot(type="png")
    return LLMCaptchaCapture(content, viewport_width, viewport_height)


async def _capture_native_hcaptcha_canvas(page: Page) -> LLMCaptchaCapture | None:
    challenge_frame = next(
        (
            frame
            for frame in getattr(page, "frames", [])
            if "hcaptcha" in frame.url.lower() and "frame=challenge" in frame.url.lower()
        ),
        None,
    )
    if challenge_frame is None:
        return None

    try:
        iframe_rect = await page.evaluate(
            """() => {
                const candidates = Array.from(document.querySelectorAll('iframe'))
                    .map((iframe) => {
                        const marker = [iframe.src, iframe.title, iframe.name]
                            .filter(Boolean).join(' ').toLowerCase();
                        if (!marker.includes('hcaptcha') || !marker.includes('frame=challenge')) {
                            return null;
                        }
                        const style = getComputedStyle(iframe);
                        const box = iframe.getBoundingClientRect();
                        if (style.display === 'none' || style.visibility === 'hidden' ||
                            Number(style.opacity || 1) === 0 || box.width < 20 || box.height < 20) {
                            return null;
                        }
                        return {x: box.left, y: box.top, width: box.width, height: box.height};
                    })
                    .filter(Boolean)
                    .sort((a, b) => b.width * b.height - a.width * a.height);
                return candidates[0] || null;
            }"""
        )
        if not isinstance(iframe_rect, dict):
            return None

        canvas = challenge_frame.locator("canvas").first
        if await canvas.count() < 1 or not await canvas.is_visible():
            return None
        canvas_data = await canvas.evaluate(
            r"""(canvas) => {
                const box = canvas.getBoundingClientRect();
                const prompt = document.querySelector('#prompt-question');
                const submit = document.querySelector('.button-submit');
                const promptText = prompt
                    ? prompt.textContent.replace(/\s+/g, ' ').trim()
                    : '';
                const label = submit
                    ? [submit.textContent, submit.getAttribute('aria-label')]
                        .filter(Boolean).join(' ').replace(/\s+/g, ' ').trim()
                    : '';
                let patternGrounding = null;
                if (/place the correct animal into the empty spot/i.test(promptText) &&
                    (/skip/i.test(label) || label.includes('跳过'))) {
                    try {
                        const probe = document.createElement('canvas');
                        probe.width = canvas.width;
                        probe.height = canvas.height;
                        const context = probe.getContext('2d', {willReadFrequently: true});
                        context.drawImage(canvas, 0, 0);
                        const pixels = context.getImageData(
                            0, 0, probe.width, probe.height
                        ).data;
                        const gridXs = [0.405, 0.55, 0.70, 0.85]
                            .map((value) => Math.round(value * probe.width));
                        const gridYs = [0.40, 0.56, 0.72, 0.88]
                            .map((value) => Math.round(value * probe.height));
                        const candidateX = Math.round(0.14 * probe.width);
                        const candidateYs = [0.38, 0.57]
                            .map((value) => Math.round(value * probe.height));
                        const radius = Math.max(20, Math.round(0.045 * probe.width));
                        const step = Math.max(1, Math.round(probe.width / 500));

                        const distance = (firstX, firstY, secondX, secondY) => {
                            let total = 0;
                            let samples = 0;
                            for (let dy = -radius; dy <= radius; dy += step) {
                                for (let dx = -radius; dx <= radius; dx += step) {
                                    if (dx * dx + dy * dy > radius * radius) continue;
                                    const ax = firstX + dx;
                                    const ay = firstY + dy;
                                    const bx = secondX + dx;
                                    const by = secondY + dy;
                                    if (ax < 0 || ay < 0 || bx < 0 || by < 0 ||
                                        ax >= probe.width || bx >= probe.width ||
                                        ay >= probe.height || by >= probe.height) continue;
                                    const first = (ay * probe.width + ax) * 4;
                                    const second = (by * probe.width + bx) * 4;
                                    total += Math.abs(pixels[first] - pixels[second]);
                                    total += Math.abs(pixels[first + 1] - pixels[second + 1]);
                                    total += Math.abs(pixels[first + 2] - pixels[second + 2]);
                                    samples += 3;
                                }
                            }
                            return samples ? total / samples : 0;
                        };

                        const rowDetails = gridYs.map((gridY) => {
                            const matrix = Array.from({length: 4}, () => Array(4).fill(0));
                            for (let first = 0; first < 4; first += 1) {
                                for (let second = first + 1; second < 4; second += 1) {
                                    const value = distance(
                                        gridXs[first], gridY, gridXs[second], gridY
                                    );
                                    matrix[first][second] = value;
                                    matrix[second][first] = value;
                                }
                            }
                            const cells = matrix.map(
                                (values) => values.reduce((sum, value) => sum + value, 0) / 3
                            );
                            const score = cells.reduce((sum, value) => sum + value, 0) / 4;
                            return {score, cells};
                        });
                        const rankedRows = [0, 1, 2, 3]
                            .sort((a, b) => rowDetails[b].score - rowDetails[a].score);
                        const targetRow = rankedRows[0];
                        const rankedColumns = [0, 1, 2, 3].sort(
                            (a, b) => rowDetails[targetRow].cells[b] -
                                rowDetails[targetRow].cells[a]
                        );
                        const targetColumn = rankedColumns[0];
                        const referenceColumn = [0, 1, 2, 3]
                            .find((column) => column !== targetColumn);
                        const candidateDistances = candidateYs.map((candidateY) => distance(
                            candidateX,
                            candidateY,
                            gridXs[referenceColumn],
                            gridYs[targetRow]
                        ));
                        const sourceIndex = candidateDistances[0] <= candidateDistances[1] ? 0 : 1;
                        const rowRatio = rowDetails[rankedRows[0]].score /
                            Math.max(1, rowDetails[rankedRows[1]].score);
                        const cellRatio = rowDetails[targetRow].cells[rankedColumns[0]] /
                            Math.max(1, rowDetails[targetRow].cells[rankedColumns[1]]);
                        const candidateRatio = Math.max(...candidateDistances) /
                            Math.max(1, Math.min(...candidateDistances));
                        if (rowRatio >= 1.4 && cellRatio >= 1.25 && candidateRatio >= 1.15) {
                            patternGrounding = {
                                sourceIndex: sourceIndex + 1,
                                targetRow: targetRow + 1,
                                targetColumn: targetColumn + 1,
                                startX: candidateX,
                                startY: candidateYs[sourceIndex],
                                endX: gridXs[targetColumn],
                                endY: gridYs[targetRow],
                                rowRatio,
                                cellRatio,
                                candidateRatio,
                            };
                        }
                    } catch (_) {}
                }
                return {
                    dataUrl: canvas.toDataURL('image/png'),
                    width: canvas.width,
                    height: canvas.height,
                    cssX: box.x,
                    cssY: box.y,
                    cssWidth: box.width,
                    cssHeight: box.height,
                    prompt: promptText,
                    submitLabel: label,
                    patternGrounding,
                };
            }"""
        )
        if not isinstance(canvas_data, dict):
            return None
        numeric_keys = ("width", "height", "cssX", "cssY", "cssWidth", "cssHeight")
        if not all(
            not isinstance(canvas_data.get(key), bool)
            and isinstance(canvas_data.get(key), (int, float))
            for key in numeric_keys
        ):
            return None
        width = int(canvas_data["width"])
        height = int(canvas_data["height"])
        css_width = float(canvas_data["cssWidth"])
        css_height = float(canvas_data["cssHeight"])
        if width < 20 or height < 20 or css_width < 20 or css_height < 20:
            return None
        data_url = canvas_data.get("dataUrl")
        if not isinstance(data_url, str) or not data_url.startswith("data:image/png;base64,"):
            return None
        content = base64.b64decode(data_url.split(",", 1)[1], validate=True)
        if not content.startswith(b"\x89PNG\r\n\x1a\n"):
            return None
        iframe_x = iframe_rect.get("x")
        iframe_y = iframe_rect.get("y")
        if (
            isinstance(iframe_x, bool)
            or not isinstance(iframe_x, (int, float))
            or isinstance(iframe_y, bool)
            or not isinstance(iframe_y, (int, float))
        ):
            return None
        grounding = canvas_data.get("patternGrounding")
        grounded_action = _parse_pattern_grounding(grounding, width, height)
        return LLMCaptchaCapture(
            content=content,
            width=width,
            height=height,
            offset_x=float(iframe_x) + float(canvas_data["cssX"]),
            offset_y=float(iframe_y) + float(canvas_data["cssY"]),
            scale_x=css_width / width,
            scale_y=css_height / height,
            source="hcaptcha_canvas",
            challenge_prompt=str(canvas_data.get("prompt") or ""),
            submit_label=str(canvas_data.get("submitLabel") or ""),
            grounded_action=grounded_action,
            grounding=grounding if isinstance(grounding, dict) else None,
        )
    except Exception:
        return None


def _parse_pattern_grounding(
    grounding: Any,
    width: int,
    height: int,
) -> LLMCaptchaAction | None:
    if not isinstance(grounding, dict):
        return None
    if grounding.get("sourceIndex") not in {1, 2}:
        return None
    if grounding.get("targetRow") not in {1, 2, 3, 4}:
        return None
    if grounding.get("targetColumn") not in {1, 2, 3, 4}:
        return None
    values = [grounding.get(key) for key in ("startX", "startY", "endX", "endY")]
    if not all(
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        for value in values
    ):
        return None
    start_x, start_y, end_x, end_y = (
        float(cast(int | float, value)) for value in values
    )
    if not (0 <= start_x <= width and 0 <= end_x <= width):
        return None
    if not (0 <= start_y <= height and 0 <= end_y <= height):
        return None
    return LLMCaptchaAction("drag", start_x, start_y, end_x, end_y)


def _output_preview(output: str, limit: int = 500) -> str:
    compact = " ".join(output.split())
    return compact if len(compact) <= limit else compact[:limit] + "..."


def _responses_endpoint(api_base: str) -> str:
    normalized = api_base.rstrip("/")
    return normalized if normalized.endswith("/responses") else f"{normalized}/responses"


def _captcha_action_error(
    decision: LLMCaptchaDecision,
    challenge_prompt: str,
) -> str | None:
    if not decision.actions or not challenge_prompt:
        return None
    prompt = challenge_prompt.strip().lower()
    expected_kind: str | None = None
    if prompt.startswith(("click", "please click", "pick ", "find all", "select ")):
        expected_kind = "click"
    elif prompt.startswith(("drag", "move ", "place ")):
        expected_kind = "drag"
    if expected_kind and decision.actions[0].kind != expected_kind:
        return (
            f"challenge instruction requires {expected_kind}, but model returned "
            f"{decision.actions[0].kind}"
        )
    return None


def _llm_captcha_prompt(
    width: int,
    height: int,
    attempt: int,
    call_number: int,
    challenge_prompt: str = "",
    submit_label: str = "",
    capture_source: str = "viewport",
) -> str:
    prompt_detail = challenge_prompt or "Read the visible hCaptcha instruction from the image."
    submit_detail = submit_label or "not detected"
    source_detail = (
        "This is the hCaptcha canvas at its native backing resolution. The prompt may be rendered "
        "outside the canvas, so use the extracted instruction below and ignore a blank or black header."
        if capture_source == "hcaptcha_canvas"
        else "This is a browser screenshot containing the visible hCaptcha UI."
    )
    return f"""Analyze the attached image and operate only the visible hCaptcha UI.
The image is exactly {width} by {height} pixels, but every returned coordinate MUST use normalized_1000
space: (0, 0) is the image's top-left and (1000, 1000) is its bottom-right. Set coordinate_space to
"normalized_1000". Never return pixel coordinates. This is overall attempt {attempt} and model call
{call_number}. The browser has already confirmed that the CAPTCHA is not complete at the time of this
screenshot, regardless of any earlier model response.

Capture details: {source_detail}
Extracted hCaptcha instruction: {prompt_detail}
Current hCaptcha submit control label: {submit_detail}

Return at most ONE action based on this exact screenshot. The browser will execute it, wait for the UI
to update, and send a fresh screenshot before any next action. For a click, use the target center for
start_x/start_y and null for end_x/end_y. For a drag, use the draggable object's center as the start and
the destination center as the end.

Follow these hCaptcha rules:
- If an unchecked hCaptcha checkbox is visible, click only its checkbox.
- If an image-selection challenge is visible, click one clearly matching tile. Work on a fresh image
  before selecting another tile because tiles can change after a click.
- If a drag challenge is visible, return one drag action.
- If the challenge selection is complete, return status "verify" with no actions. The browser will
  click the hCaptcha Verify or Check control by DOM; that control may be outside the attached canvas.
- Verify is a CAPTCHA control. Never click account, registration, login, navigation, browser, or other
  outer-page submit controls.

Return "solved" with no actions only when a green checked hCaptcha checkbox or explicit success state is
visible and no challenge dialog remains. A visible Verify button is not success. If you can describe or
name the correct target, you MUST return its click or drag coordinates with status "actions"; never
describe an intended action only in message. If uncertain, choose the best visible CAPTCHA action rather
than returning no action. In a drag puzzle, the destination must be the center of the matching silhouette
or a currently empty target cell, never an occupied animal. Keep reasoning brief, keep coordinates inside
the image, and return only the requested JSON object."""


def _responses_output_text(payload: dict[str, Any]) -> str:
    direct = payload.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()

    chunks: list[str] = []
    output = payload.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "refusal":
                    refusal = part.get("refusal")
                    raise RuntimeError(
                        f"Responses API refused the image request: {str(refusal)[:200]}"
                    )
                text = part.get("text")
                if isinstance(text, str) and text.strip():
                    chunks.append(text.strip())
    if chunks:
        return "\n".join(chunks)

    error = payload.get("error")
    if error:
        raise RuntimeError(f"Responses API error: {str(error)[:300]}")
    raise RuntimeError("Responses API response contained no output text")


def _parse_llm_decision(text: str, width: int, height: int) -> LLMCaptchaDecision:
    clean = text.strip()
    try:
        payload = json.loads(clean)
    except json.JSONDecodeError:
        payload = None
        decoder = json.JSONDecoder()
        for index, character in enumerate(clean):
            if character != "{":
                continue
            try:
                candidate, _ = decoder.raw_decode(clean[index:])
            except json.JSONDecodeError:
                continue
            if (
                isinstance(candidate, dict)
                and "status" in candidate
                and "actions" in candidate
            ):
                payload = candidate
                break
        if payload is None:
            raise ValueError("LLM captcha output is not valid JSON")
    if not isinstance(payload, dict):
        raise ValueError("LLM captcha output must be a JSON object")

    status = payload.get("status")
    if status not in {"actions", "verify", "solved", "failed"}:
        raise ValueError("LLM captcha status must be actions, verify, solved, or failed")
    raw_actions = payload.get("actions")
    if not isinstance(raw_actions, list):
        raise ValueError("LLM captcha actions must be an array")
    ignored_action_count = max(0, len(raw_actions) - LLM_MAX_ACTIONS_PER_CALL)
    raw_actions = raw_actions[:LLM_MAX_ACTIONS_PER_CALL]
    if raw_actions and status != "actions":
        status = "actions"
    reported_coordinate_space = payload.get("coordinate_space")
    if reported_coordinate_space not in {None, "pixels", "normalized_1000"}:
        raise ValueError("LLM captcha coordinate_space is invalid")

    actions: list[LLMCaptchaAction] = []
    coordinate_space = "pixels"
    for index, raw_action in enumerate(raw_actions, 1):
        if not isinstance(raw_action, dict):
            raise ValueError(f"LLM captcha action {index} must be an object")
        kind = raw_action.get("kind")
        if kind not in {"click", "drag"}:
            raise ValueError(f"LLM captcha action {index} has an invalid kind")
        start_x = _raw_model_coordinate(raw_action.get("start_x"), index, "start_x")
        start_y = _raw_model_coordinate(raw_action.get("start_y"), index, "start_y")
        if kind == "drag":
            end_x = _raw_model_coordinate(raw_action.get("end_x"), index, "end_x")
            end_y = _raw_model_coordinate(raw_action.get("end_y"), index, "end_y")
        else:
            end_x = None
            end_y = None
        coordinate_pairs = [(start_x, width), (start_y, height)]
        if end_x is not None and end_y is not None:
            coordinate_pairs.extend(((end_x, width), (end_y, height)))
        use_normalized = reported_coordinate_space == "normalized_1000"
        if not use_normalized and any(
            value > limit for value, limit in coordinate_pairs
        ):
            if any(value > 1000 for value, _ in coordinate_pairs):
                raise ValueError(
                    f"LLM captcha action {index} coordinates are outside the viewport"
                )
            use_normalized = True
        if use_normalized:
            if any(value > 1000 for value, _ in coordinate_pairs):
                raise ValueError(
                    f"LLM captcha action {index} normalized coordinates exceed 1000"
                )
            coordinate_space = "normalized_1000"
            start_x = round(start_x / 1000 * width, 2)
            start_y = round(start_y / 1000 * height, 2)
            if end_x is not None and end_y is not None:
                end_x = round(end_x / 1000 * width, 2)
                end_y = round(end_y / 1000 * height, 2)
        actions.append(LLMCaptchaAction(kind, start_x, start_y, end_x, end_y))

    message = payload.get("message", "")
    if not isinstance(message, str):
        raise ValueError("LLM captcha message must be a string")
    return LLMCaptchaDecision(
        status,
        tuple(actions),
        message,
        ignored_action_count=ignored_action_count,
        coordinate_space=coordinate_space,
    )


def _raw_model_coordinate(value: Any, action_index: int, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"LLM captcha action {action_index} {field} must be numeric")
    coordinate = float(value)
    if not math.isfinite(coordinate) or coordinate < 0:
        raise ValueError(f"LLM captcha action {action_index} {field} is outside the viewport")
    return round(coordinate, 2)


async def _apply_llm_action(
    page: Page,
    action: LLMCaptchaAction,
    offset_x: float = 0,
    offset_y: float = 0,
    scale_x: float = 1,
    scale_y: float = 1,
) -> None:
    start_x = action.start_x * scale_x + offset_x
    start_y = action.start_y * scale_y + offset_y
    if action.kind == "click":
        await page.mouse.click(start_x, start_y, delay=80)
        return
    assert action.end_x is not None and action.end_y is not None
    end_x = action.end_x * scale_x + offset_x
    end_y = action.end_y * scale_y + offset_y
    distance = math.hypot(end_x - start_x, end_y - start_y)
    steps = max(6, min(30, int(distance / 12)))
    await page.mouse.move(start_x, start_y)
    await page.mouse.down()
    try:
        await page.mouse.move(end_x, end_y, steps=steps)
    finally:
        await page.mouse.up()


async def _click_hcaptcha_submit(page: Page, timeout_seconds: float = 3) -> bool:
    poll_seconds = 0.2
    attempts = max(1, math.ceil(timeout_seconds / poll_seconds))
    skip_labels = ("skip", "跳过")
    for attempt in range(attempts):
        for frame in getattr(page, "frames", []):
            if "hcaptcha" not in frame.url.lower() or "frame=challenge" not in frame.url.lower():
                continue
            button = frame.locator(".button-submit").first
            try:
                if await button.count() < 1 or not await button.is_visible():
                    continue
                label = await button.evaluate(
                    r"""(element) => [element.textContent, element.getAttribute('aria-label')]
                        .filter(Boolean).join(' ').replace(/\s+/g, ' ').trim().toLowerCase()"""
                )
                if not isinstance(label, str) or not label:
                    continue
                if any(skip_label in label for skip_label in skip_labels):
                    continue
                if await button.get_attribute("aria-disabled") == "true":
                    continue
                await button.click(delay=80, timeout=5000)
                return True
            except Exception:
                continue
        if attempt < attempts - 1:
            await asyncio.sleep(poll_seconds)
    return False


async def _is_captcha_target_enabled(page: Page) -> bool:
    result = await page.evaluate(
        """() => {
            const register = document.querySelector('#register_button');
            return register
                ? !register.disabled && register.getAttribute('aria-disabled') !== 'true'
                : false;
        }"""
    )
    return bool(result)


async def _reset_hcaptcha(page: Page) -> bool:
    try:
        result = await page.evaluate(
            """() => {
                try {
                    if (window.hcaptcha && typeof window.hcaptcha.reset === 'function') {
                        window.hcaptcha.reset();
                        return true;
                    }
                } catch (_) {}
                return false;
            }"""
        )
        return bool(result)
    except Exception:
        return False


# ---------------------------------------------------------------------------
#  sitekey 捕获（render=explicit 模式下 DOM 无 sitekey，只能从网络请求获取）
# ---------------------------------------------------------------------------

_captured_sitekey: str | None = None


def reset_captcha_state() -> None:
    """重置模块级缓存，供批量注册时每个新账号使用。"""
    global _captured_sitekey
    _captured_sitekey = None


def start_capturing_sitekey(page: Page) -> None:
    """注册网络请求监听器，从 checksiteconfig 请求中捕获 hCaptcha sitekey。

    必须在 create-account 页加载前调用。
    """
    def _on_request(req):
        global _captured_sitekey
        if _captured_sitekey:
            return
        url = req.url
        if "checksiteconfig" in url and "sitekey=" in url:
            try:
                sk = parse_qs(urlparse(url).query).get("sitekey", [None])[0]
                if sk:
                    _captured_sitekey = sk
                    print(f"  sitekey captured: {sk}")
            except Exception:
                pass

    page.on("request", _on_request)


async def _get_site_key(page: Page) -> str | None:
    """获取 sitekey（仅从网络请求缓存中读取）。"""
    if _captured_sitekey:
        return _captured_sitekey
    # 等待网络请求捕获（hCaptcha iframe 可能还在加载）
    for _ in range(30):
        if _captured_sitekey:
            return _captured_sitekey
        await asyncio.sleep(1)
    return None


# ---------------------------------------------------------------------------
#  token 注入（通过拦截的 Angular 回调直接触发 onSuccess）
# ---------------------------------------------------------------------------


async def _inject_hcaptcha_token(page: Page, token: str) -> None:
    """调用拦截的 __hCaptchaCallback 触发 Angular onSuccess，使 #register_button enable。

    回调由 main.py 的 _ensure_hcaptcha_hook 通过 addInitScript 在
    hcaptcha.render 调用时捕获到 window.__hCaptchaCallback。
    """
    result = await page.evaluate(
        r"""(token) => {
            if (typeof window.__hCaptchaCallback === 'function') {
                window.__hCaptchaCallback(token);
                return true;
            }
            return false;
        }""",
        token,
    )
    print(f"  callback called: {result}")


# ---------------------------------------------------------------------------
#  辅助
# ---------------------------------------------------------------------------


async def _is_register_button_enabled(page: Page) -> bool:
    """检查 #register_button 是否 enabled（hCaptcha 通过后按钮才会 enable）。"""
    result = await page.evaluate(
        """() => {
            const btn = document.querySelector('#register_button');
            return btn ? !btn.disabled : false;
        }"""
    )
    return bool(result)


def _site_referer(website_url: str) -> str:
    parsed = urlparse(website_url)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}/"
    return website_url


def _response_json(response: requests.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        return {"status_code": response.status_code, "text": response.text}
    return data if isinstance(data, dict) else {"data": data}


def _extract_hcaptcha_token(data: dict[str, Any]) -> str | None:
    token = data.get("gRecaptchaResponse") or data.get("token")
    return str(token) if token else None


def build_captcha_solver(config: CaptchaConfig) -> CaptchaSolver:
    if config.mode == "manual":
        return ManualCaptchaSolver()
    if config.mode == "yescaptcha":
        if not config.yescaptcha_client_key:
            raise ValueError("yescaptcha_client_key is required")
        return YesCaptchaSolver(
            client_key=config.yescaptcha_client_key,
            api_url=config.yescaptcha_api_url,
            poll_interval_seconds=config.poll_interval_seconds,
            timeout_seconds=config.timeout_seconds,
        )
    if config.mode == "llm":
        if not config.llm_model or not config.llm_api_key:
            raise ValueError("llm_model and llm_api_key are required")
        return LLMCaptchaSolver(
            model=config.llm_model,
            api_base=config.llm_api_base,
            api_key=config.llm_api_key,
            timeout_seconds=config.timeout_seconds,
            reasoning_effort=config.llm_reasoning_effort,
            call_delay_seconds=config.llm_call_delay_seconds,
            action_delay_seconds=config.llm_action_delay_seconds,
            calls_per_attempt=config.llm_calls_per_attempt,
            max_attempts=config.llm_max_attempts,
            max_output_tokens=config.llm_max_output_tokens,
            artifact_dir=config.llm_artifact_dir,
        )
    if config.mode == "captcharun":
        if not config.captcharun_token:
            raise ValueError("captcharun_token is required")
        return CaptchaRunSolver(
            token=config.captcharun_token,
            api_url=config.captcharun_api_url,
            poll_interval_seconds=config.poll_interval_seconds,
            timeout_seconds=config.timeout_seconds,
        )
    raise ValueError(f"Unsupported captcha mode: {config.mode}")
