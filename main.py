#!/usr/bin/env python3
"""
nvidia-register — 注册 build.nvidia.com 账号并创建 AI_PLAYGROUNDS_KEY

完整流程（基于真实页面链路，全部实测确认）：
  创建临时邮箱 → build.nvidia.com 填邮箱 → create-account 页填密码 + 过 hCaptcha
  → 验证码页真实键盘输入 → 通行密钥引导页(自动点"稍后再说"跳过)
  → 同意/快完成页 → (session 丢失) 邮箱+密码重新登录
  → 创建组织跳过手机验证 → 调 NGC API 建 key → 记录到 CSV

用法:
  pip install -r requirements.txt
  playwright install chromium

  python main.py --init       # 生成 config.toml 配置文件
  # 编辑 config.toml 填入你的信息
  python main.py              # 交互式询问注册数量
  python main.py -n 5         # 直接注册 5 个账号（不询问）
  python main.py --count 3    # 同上

配置文件: config.toml（见 config.toml.example 或 --init 生成）
Ctrl+C  优雅退出：完成当前正在注册的账号后退出。
"""

import asyncio
import json
import signal
import sys
import time
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from playwright.async_api import (
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from config import AppConfig, describe_config, init_config, load_config
from captcha import build_captcha_solver, reset_captcha_state, start_capturing_sitekey
from email_providers import TempEmailProvider, build_email_provider
from passwords import generate_password
from records import append_account_record


FAILURE_ARTIFACT_DIR = Path(__file__).resolve().parent / "failure_artifacts"
ACCOUNT_ATTEMPTS = 2

# ---------------------------------------------------------------------------
#  CLI
# ---------------------------------------------------------------------------

def _parse_cli_options(argv: list[str]) -> tuple[int | None, bool | None, int | None]:
    """解析注册数量、浏览器显示模式和并发数覆盖项。"""
    args = argv[1:]
    count: int | None = None
    headless: bool | None = None
    concurrency: int | None = None
    i = 0
    while i < len(args):
        if args[i] in ("-n", "--count") and i + 1 < len(args):
            try:
                count = int(args[i + 1])
            except ValueError:
                print(f"Error: {args[i]} requires a number, got: {args[i + 1]}")
                sys.exit(1)
            i += 2
            continue
        if args[i] in ("-j", "--concurrency") and i + 1 < len(args):
            try:
                concurrency = int(args[i + 1])
            except ValueError:
                print(f"Error: {args[i]} requires a number, got: {args[i + 1]}")
                sys.exit(1)
            i += 2
            continue
        if args[i] == "--headless":
            headless = True
        elif args[i] == "--headed":
            headless = False
        i += 1
    return count, headless, concurrency

def main_cli() -> None:
    # 非 TTY（重定向/后台/IDE）下 Python 默认块缓冲，输出会被憋住；
    # 设为行缓冲保证每个 print 实时可见
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass

    args = sys.argv[1:]

    if args and args[0] == "--init":
        init_config()
        return

    config = load_config()

    count, headless_override, concurrency_override = _parse_cli_options(sys.argv)
    if headless_override is not None or concurrency_override is not None:
        config = replace(
            config,
            browser=replace(
                config.browser,
                headless=(
                    headless_override
                    if headless_override is not None
                    else config.browser.headless
                ),
                concurrency=(
                    concurrency_override
                    if concurrency_override is not None
                    else config.browser.concurrency
                ),
            ),
        )
    if count is None:
        # 交互式询问
        try:
            raw = input("注册账号数量 (默认 1): ").strip()
            count = int(raw) if raw else 1
        except (ValueError, EOFError):
            count = 1
    if count < 1:
        print("数量必须 >= 1")
        return

    try:
        asyncio.run(run(config, count))
    except KeyboardInterrupt:
        print("\n\nInterrupted. Goodbye!")

# ---------------------------------------------------------------------------
#  注册流程
# ---------------------------------------------------------------------------

# Ctrl+C 优雅退出标志
_shutdown = False

def _handle_sigint():
    global _shutdown
    if _shutdown:
        # 第二次 Ctrl+C → 强制退出
        print("\n\nForce exit!")
        sys.exit(1)
    _shutdown = True
    print("\n\nCtrl+C received. Will exit after current account finishes...")

async def run(config: AppConfig, count: int = 1) -> None:
    global _shutdown
    _shutdown = False
    if config.browser.headless and config.captcha.mode == "manual":
        raise ValueError(
            "Headless browser mode requires an automatic captcha mode "
            "(llm, yescaptcha, or captcharun); manual mode cannot display the challenge"
        )

    if not 1 <= config.browser.concurrency <= 10:
        raise ValueError("Browser concurrency must be between 1 and 10")

    print("=" * 60)
    print("NVIDIA Register + API Key Creator")
    print("-" * 60)
    describe_config(config)
    print(f"  注册数量: {count}")
    print("=" * 60)
    print("  (Ctrl+C 优雅退出：完成当前账号后停止)\n")

    # 注册信号处理（跨平台）
    loop = asyncio.get_running_loop()
    try:
        loop.add_signal_handler(signal.SIGINT, _handle_sigint)
    except NotImplementedError:
        # Windows 不支持 add_signal_handler，用 signal.signal 兜底
        signal.signal(signal.SIGINT, lambda *_: _handle_sigint())

    async with async_playwright() as p:
        results = await _run_accounts(p, config, count)

    success_count = sum(result is not None for result in results)
    fail_count = len(results) - success_count

    # 汇总
    print("\n" + "=" * 60)
    print(f"完成! 成功: {success_count}, 失败: {fail_count}, 总计: {success_count + fail_count}")
    print("=" * 60)


async def _run_accounts(p, config: AppConfig, count: int) -> list[str | None]:
    """Run isolated account sessions with a bounded worker pool."""
    next_index = 0
    index_lock = asyncio.Lock()
    record_lock = asyncio.Lock()
    launch_lock = asyncio.Lock()
    last_launch_at = 0.0
    llm_request_semaphore = asyncio.Semaphore(config.captcha.llm_max_concurrency)
    results: list[str | None] = []

    async def wait_for_launch_slot() -> None:
        nonlocal last_launch_at
        async with launch_lock:
            delay = config.browser.launch_stagger_seconds - (
                time.monotonic() - last_launch_at
            )
            if last_launch_at and delay > 0:
                await asyncio.sleep(delay)
            last_launch_at = time.monotonic()

    async def worker() -> None:
        nonlocal next_index
        while True:
            async with index_lock:
                if _shutdown or next_index >= count:
                    return
                account_index = next_index
                next_index += 1

            print(f"\n{'#' * 60}")
            print(f"# 账号 {account_index + 1} / {count}")
            print(f"{'#' * 60}")
            try:
                result = None
                for account_attempt in range(1, ACCOUNT_ATTEMPTS + 1):
                    await wait_for_launch_slot()
                    result = await _register_one(
                        p,
                        config,
                        account_index=account_index,
                        record_lock=record_lock,
                        llm_request_semaphore=llm_request_semaphore,
                    )
                    if result is not None or account_attempt >= ACCOUNT_ATTEMPTS:
                        break
                    print(
                        f"  Account {account_index + 1} retrying after failed attempt "
                        f"({account_attempt}/{ACCOUNT_ATTEMPTS})..."
                    )
                    await asyncio.sleep(2)
            except Exception as exc:
                print(f"\n  Account {account_index + 1} failed unexpectedly: {exc}")
                result = None
            results.append(result)

    worker_count = min(count, config.browser.concurrency)
    await asyncio.gather(*(worker() for _ in range(worker_count)))
    return results

async def _register_one(
    p,
    config: AppConfig,
    account_index: int,
    record_lock: asyncio.Lock,
    llm_request_semaphore: asyncio.Semaphore,
) -> str | None:
    """单个账号的完整注册流程。返回 api_key 或 None。"""
    email_provider = build_email_provider(config)
    captcha_solver = build_captcha_solver(
        config.captcha,
        request_semaphore=llm_request_semaphore,
    )
    password = generate_password(12)
    browser = await p.chromium.launch(
        channel="chromium" if config.browser.headless else None,
        headless=config.browser.headless,
        args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
    )
    try:
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                _chromium_user_agent(browser.version)
                if config.browser.headless
                else None
            ),
        )
        page = await context.new_page()
        reset_captcha_state(page)
    except Exception:
        await browser.close()
        raise

    stage = "email_creation"
    try:
        # 1. 创建临时邮箱
        inbox_name = f"nv{time.time_ns() % 10**12:012d}{account_index:02d}"
        try:
            inbox = await asyncio.to_thread(email_provider.create_inbox, inbox_name)
        except Exception as exc:
            print(f"  Email creation failed: {exc}")
            return None
        print(f"\n[1] Email: {inbox.address}")

        # 2. 打开 build.nvidia.com，接受 cookie 弹窗
        stage = "build_entry"
        print("[2] Opening build.nvidia.com...")
        await page.goto("https://build.nvidia.com/", wait_until="domcontentloaded", timeout=90000)
        await _accept_cookie_banner(page, timeout_seconds=10)

        # 3. 点击 Login 打开登录弹窗
        stage = "sign_in_modal"
        print("[3] Open sign-in modal...")
        if not await _open_signin_modal(page):
            print("  Login button not found")
            await _print_clickable_snapshot(page)
            return None

        # 4. 填邮箱 → Next（跳转到 login.nvgs.nvidia.com/v1/create-account）
        stage = "email_submission"
        print("[4] Submit email...")
        start_capturing_sitekey(page)
        await _ensure_hcaptcha_hook(page)
        if not await _submit_email_step(page, inbox.address):
            print("  Failed at email step")
            await _print_clickable_snapshot(page)
            return None

        # 5. 注册（填密码 → 过 hCaptcha → 提交 → 验证码）
        stage = "registration"
        ok = await register_account(page, inbox, password, email_provider, captcha_solver, config)
        if not ok:
            print("\nRegistration failed")
            return None

        # 6. 状态机处理注册后跳转，直到 session 有效并建 key
        stage = "api_key_creation"
        api_key = await finalize_and_create_key(page, inbox, password, config)

        # 7. 记录到 CSV
        if api_key:
            stage = "recording"
            async with record_lock:
                await asyncio.to_thread(
                    append_account_record,
                    path=config.nvidia.output_csv,
                    email=inbox.address,
                    password=password,
                    api_key=api_key,
                )
            print(f"  Record saved to: {config.nvidia.output_csv}")
            print(f"\n  ✓ {inbox.address} → {api_key[:30]}...")
            stage = "completed"
            return api_key
        else:
            print("\nRegistration succeeded but API Key creation failed")
            return None
    finally:
        if stage != "completed":
            await _save_failure_artifact(
                page,
                account_index=account_index,
                stage=stage,
                detail=getattr(captcha_solver, "last_error", None),
            )
        await _close_browser(browser, config.browser.close_delay_seconds)


def _chromium_user_agent(version: str) -> str:
    """Return the regular Linux Chrome UA for Playwright's full headless Chromium."""
    return (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{version} Safari/537.36"
    )


def _redact_artifact_url(url: str) -> str:
    """Keep page identity without persisting emails or signed query values."""
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


async def _save_failure_artifact(
    page: Page,
    account_index: int,
    stage: str,
    detail: str | None,
) -> None:
    session_dir = FAILURE_ARTIFACT_DIR / (
        f"{time.strftime('%Y%m%d-%H%M%S')}-account-{account_index + 1:02d}-"
        f"{uuid4().hex[:8]}"
    )
    try:
        session_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
        screenshot = session_dir / "page.png"
        await page.screenshot(path=str(screenshot), type="png", timeout=5000)
        screenshot.chmod(0o600)
        metadata = session_dir / "metadata.json"
        metadata.write_text(
            json.dumps(
                {
                    "account_index": account_index + 1,
                    "stage": stage,
                    "url": _redact_artifact_url(page.url),
                    "detail": detail,
                    "time": time.time(),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        metadata.chmod(0o600)
        print(f"  Failure artifact saved: {session_dir}")
    except Exception as exc:
        print(f"  Failure artifact could not be saved: {exc}")

# ---------------------------------------------------------------------------
#  子流程
# ---------------------------------------------------------------------------

async def _accept_cookie_banner(page: Page, timeout_seconds: float = 3) -> None:
    """Dismiss a late-loading OneTrust overlay without blocking page startup."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        selectors = (
            "#onetrust-accept-btn-handler:visible",
            "#onetrust-reject-all-handler:visible",
            "#onetrust-consent-sdk button:visible",
            "button:visible",
        )
        for selector in selectors:
            buttons = page.locator(selector)
            try:
                count = await buttons.count()
            except Exception:
                continue
            for index in range(min(count, 12)):
                button = buttons.nth(index)
                try:
                    label = " ".join(
                        (
                            await button.inner_text(timeout=300)
                        ).split()
                    ).lower()
                    aria_label = (
                        await button.get_attribute("aria-label") or ""
                    ).lower()
                    text = f"{label} {aria_label}"
                    if not any(
                        marker in text
                        for marker in (
                            "accept all",
                            "accept",
                            "reject optional",
                            "拒绝",
                            "接受",
                            "同意",
                        )
                    ):
                        continue
                    if "manage settings" in text or "管理设置" in text:
                        continue
                    await button.click(force=True, timeout=1500)
                    print("  cookie accepted")
                    try:
                        await page.locator("#onetrust-consent-sdk").wait_for(
                            state="hidden",
                            timeout=3000,
                        )
                    except PlaywrightTimeoutError:
                        pass
                    await asyncio.sleep(0.5)
                    return
                except Exception:
                    continue
        await asyncio.sleep(0.25)

async def _open_signin_modal(page: Page) -> bool:
    """点击 header 的 Login，打开 signin 弹窗。

    实测：点 Login 后先出现临时弹窗，1~2 秒后页面自动刷新出真正有效弹窗。
    """
    deadline = time.monotonic() + 45
    reported_missing = False
    reload_count = 0
    next_reload_at = time.monotonic() + 6
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        await _accept_cookie_banner(page, timeout_seconds=1)
        login_selectors = (
            page.get_by_role("button", name="Login").filter(visible=True).first,
            page.locator(
                "button[data-nvtrack-nav-object='login-button']:visible"
            ).first,
        )
        clicked = False
        for login in login_selectors:
            try:
                if await login.count() < 1:
                    continue
                await login.wait_for(
                    state="visible",
                    timeout=max(1000, min(5000, int(remaining * 1000))),
                )
                await login.click(timeout=5000)
                clicked = True
                break
            except Exception as exc:
                if not reported_missing:
                    print(f"  Login click failed: {exc}")
                    reported_missing = True
                await _accept_cookie_banner(page, timeout_seconds=1)

        # The first modal can be replaced by a fresh DOM after the click.
        if clicked:
            await _wait_for_stable_email_input(page)
            if await page.locator('input[name="email"]:visible').count() > 0:
                return True
        if reload_count < 2 and time.monotonic() >= next_reload_at:
            try:
                await page.reload(
                    wait_until="domcontentloaded",
                    timeout=max(5000, min(15000, int(remaining * 1000))),
                )
                reload_count += 1
                print(f"  build page reloaded ({reload_count}/2)")
            except Exception as exc:
                if not reported_missing:
                    print(f"  build page reload failed: {exc}")
                    reported_missing = True
            next_reload_at = time.monotonic() + 6
        else:
            await asyncio.sleep(1)
    return False

async def _wait_for_stable_email_input(page: Page, settle_seconds: float = 3.0) -> None:
    """等待 signin 弹窗自动刷新完成，直到可见 email 输入框稳定。"""
    deadline = time.time() + 20
    stable_since = None
    while time.time() < deadline:
        try:
            visible = await page.locator('input[name="email"]:visible').count()
        except Exception:
            visible = 0
        if visible >= 1:
            if stable_since is None:
                stable_since = time.time()
            elif time.time() - stable_since >= settle_seconds:
                return
        else:
            stable_since = None
        await asyncio.sleep(0.5)

async def _submit_email_step(page: Page, email: str) -> bool:
    """在有效 signin 弹窗填邮箱并点 Next，跳转到 create-account 页。"""
    email_input = page.locator('input[name="email"]:visible').first
    try:
        await email_input.wait_for(state="visible", timeout=15000)
    except Exception:
        return False

    await email_input.click()
    await email_input.press_sequentially(email, delay=50)
    await asyncio.sleep(0.3)

    next_btn = page.get_by_role("button", name="Next").filter(visible=True).first
    try:
        await next_btn.wait_for(state="visible", timeout=5000)
        for _ in range(20):
            if await next_btn.is_enabled():
                await next_btn.click()
                print("  Next clicked")
                break
            await asyncio.sleep(0.5)
        else:
            print("  Next stayed disabled")
            return False
    except Exception:
        return False

    # The broad /login URL is also the email step itself. Wait for the actual
    # create-account route or for its password form before returning; otherwise
    # concurrent headless sessions can start registration against the old page.
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        try:
            password_visible = await page.locator(
                "#registration_password:visible"
            ).count() > 0
        except Exception:
            password_visible = False
        if password_visible or "create-account" in page.url:
            print(f"  navigated to: {page.url[:80]}")
            return True
        await asyncio.sleep(0.5)
    print(f"  create-account page did not appear: {page.url[:80]}")
    return False

async def register_account(
    page: Page,
    inbox,
    password: str,
    email_provider: TempEmailProvider,
    captcha_solver,
    config: AppConfig,
) -> bool:
    """create-account 页：填密码 → 过 hCaptcha → 点 #register_button → 验证码页真实键盘输入。"""
    # [1/4] 等待密码字段并填写
    print("\n[1/4] Fill password...")
    try:
        await page.locator("#registration_password").wait_for(state="visible", timeout=45000)
    except PlaywrightTimeoutError:
        print("  password field never appeared")
        await _print_clickable_snapshot(page)
        return False

    await page.fill("#registration_password", password)
    await page.fill("#registration_passwordConfirm", password)
    # 保持登录（可选）
    try:
        checkbox = page.locator("#stay_signin_checkbox_v2-input")
        if await checkbox.count() > 0 and not await checkbox.is_checked():
            await checkbox.check()
    except Exception:
        pass
    print("  password OK")

    # [2/4] 过 hCaptcha（token 到位后 #register_button 才 enable）
    if not await captcha_solver.solve(page):
        print("  Captcha failed")
        return False

    print("\n[2/4] Submit registration (#register_button)...")
    register_btn = page.locator("#register_button")
    try:
        await register_btn.wait_for(state="visible", timeout=15000)
        # 等待按钮 enable（token 生效后）
        for _ in range(30):
            if await register_btn.is_enabled():
                break
            await asyncio.sleep(1)
    except Exception as exc:
        print(f"  #register_button not ready: {exc}")
        await _print_clickable_snapshot(page)
        return False

    # 点击前记录已有邮件，避免把上一封验证码当成这次注册的结果。
    try:
        known_message_ids = await asyncio.to_thread(
            email_provider.snapshot_message_ids,
            inbox,
        )
    except Exception as exc:
        print(f"  mailbox snapshot failed: {exc}")
        return False
    print(f"  Mailbox baseline: {len(known_message_ids)} existing message(s)")

    try:
        await register_btn.click()
    except Exception as exc:
        print(f"  #register_button not clickable: {exc}")
        await _print_clickable_snapshot(page)
        return False

    # [3/4] 等待验证码邮件
    print("\n[3/4] Waiting for verification code email...")
    code = await asyncio.to_thread(
        email_provider.poll_verification_code,
        inbox,
        timeout_seconds=config.captcha.timeout_seconds,
        known_message_ids=known_message_ids,
    )
    if not code:
        print("  No verification code received")
        return False
    print(f"  Code: {code}")

    # 等验证码输入页出现（6 个 number 输入框）
    if not await _wait_for_verification_inputs(page, timeout_seconds=45):
        print("  verification inputs not detected")
        await _print_clickable_snapshot(page)
        return False

    # [4/4] 真实键盘输入验证码（React 受控组件，JS setValue 无效）
    print("\n[4/4] Type verification code...")
    if not await _type_verification_code(page, code):
        print("  failed to type verification code")
        return False

    # 点“继续”提交验证码，并确认页面真正接受了该验证码。
    verification_url = page.url
    if not await _click_continue(page):
        print("  verification submit button not available")
        return False
    if not await _wait_for_verification_submission(page, verification_url):
        print("  verification code was rejected or submission timed out")
        return False

    # 验证码通过后 NVIDIA 会插入"创建通行密钥"引导页（/v1/passkey/prompt-setup），
    # 先自动跳过它（点"稍后再说" + 确认对话框"确定"），否则流程会卡死在该页。
    await _skip_passkey_prompt_if_present(page)
    print("\nRegistration submitted!")
    return True

async def _wait_for_verification_inputs(page: Page, timeout_seconds: int) -> bool:
    """等待 6 个验证码数字输入框出现。"""
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if await page.locator('input[type="number"]').count() >= 6:
            print("  verification inputs appeared")
            return True
        await asyncio.sleep(1)
    return False

async def _type_verification_code(page: Page, code: str) -> bool:
    """点第一个数字框后逐字符键盘输入，触发 React 状态。"""
    inputs = page.locator('input[type="number"]')
    count = await inputs.count()
    if count < 6:
        return False
    # 聚焦第一个框
    await inputs.first.click()
    for index, digit in enumerate(code[:count]):
        try:
            await inputs.nth(index).click()
        except Exception:
            pass
        await page.keyboard.type(digit, delay=80)
        await asyncio.sleep(0.15)
    await asyncio.sleep(0.5)
    return True


async def _wait_for_verification_submission(
    page: Page,
    original_url: str,
    timeout_seconds: int = 20,
) -> bool:
    deadline = time.monotonic() + timeout_seconds
    invalid_markers = (
        "验证码无效",
        "验证码已过期",
        "invalid verification code",
        "verification code is invalid",
        "verification code has expired",
    )
    while time.monotonic() < deadline:
        try:
            body_text = (await page.locator("body").inner_text()).lower()
            if any(marker in body_text for marker in invalid_markers):
                return False
            if page.url != original_url:
                return True
            if await page.locator('input[type="number"]:visible').count() < 6:
                return True
        except Exception:
            if page.url != original_url:
                return True
        await asyncio.sleep(0.5)
    return False


async def _click_continue(page: Page) -> bool:
    """点验证码/同意页的主推进按钮（继续 / 提交）。"""
    for name in ("继续", "提交"):
        try:
            btn = page.get_by_role("button", name=name).first
            if await btn.count() > 0 and await btn.is_enabled():
                await btn.click(timeout=5000)
                print(f"  clicked [{name}]")
                return True
        except Exception:
            continue
    return False

# 通行密钥引导页（/v1/passkey/prompt-setup）的跳过按钮与二次确认对话框按钮名。
# 页面语言随 locale 变化，所以中英文名称都试一遍。
_PASSKEY_SKIP_BUTTON_NAMES = ("稍后再说", "Maybe later", "Not now", "Skip")
_CONFIRM_BUTTON_NAMES = ("确定", "OK", "Confirm", "Yes", "是")

async def _click_button_by_names(page: Page, names: tuple[str, ...], timeout_ms: int = 5000) -> str | None:
    """按可访问名依次点击可见且可用的按钮，返回命中的名称；全部未命中返回 None。"""
    for name in names:
        try:
            button = page.get_by_role("button", name=name).filter(visible=True).first
            if await button.count() > 0 and await button.is_enabled():
                await button.click(timeout=timeout_ms)
                return name
        except Exception:
            continue
    return None

async def _confirm_skip_dialog(page: Page) -> bool:
    """点掉"确定要跳过设置通行密钥吗"确认对话框。"""
    for _ in range(10):
        clicked = await _click_button_by_names(page, _CONFIRM_BUTTON_NAMES, timeout_ms=3000)
        if clicked:
            print(f"  passkey 确认对话框：已点击 [{clicked}]")
            return True
        await asyncio.sleep(0.5)
    return False

async def _skip_passkey_prompt(page: Page) -> bool:
    """跳过"创建通行密钥"引导页（/v1/passkey/prompt-setup）。

    NVIDIA 在邮箱验证之后新增了这一步，页面上有两个按钮：
      #cancelSetupSelect_btn → "稍后再说"（我们要点的）
      #setUpPasskey_btn      → "立即创建"（会拉起 WebAuthn，自动化环境无法完成）

    点"稍后再说"之后还会弹一个确认对话框（"您确定要跳过设置通行密钥吗？"），
    必须再点"确定"才会真正离开该页。
    """
    try:
        skip_btn = page.locator("#cancelSetupSelect_btn")
        if await skip_btn.count() > 0:
            await skip_btn.first.click(timeout=5000)
            print("  passkey 引导页：已点击 [稍后再说]")
            await _confirm_skip_dialog(page)
            return True
    except Exception:
        pass

    clicked = await _click_button_by_names(page, _PASSKEY_SKIP_BUTTON_NAMES)
    if clicked:
        print(f"  passkey 引导页：已点击 [{clicked}]")
        await _confirm_skip_dialog(page)
        return True

    print("  passkey 引导页：未找到跳过按钮")
    await _print_clickable_snapshot(page)
    return False

async def _skip_passkey_prompt_if_present(page: Page, wait_seconds: int = 15) -> bool:
    """等待并跳过可能出现的通行密钥引导页；没出现就直接返回，不阻塞后续流程。

    验证码通过后页面可能还没跳转到 /v1/passkey/prompt-setup，所以先轮询 URL 与
    DOM；若已经进入同意页/组织页等后续环节，说明本次没有通行密钥引导页，提前结束。
    """
    deadline = time.monotonic() + wait_seconds
    moved_on_markers = ("consent", "static-login", "select-account", "cloudaccounts")
    while time.monotonic() < deadline:
        url_now = page.url
        if "passkey" in url_now:
            return await _skip_passkey_prompt(page)
        # 已经跳到后续页面，说明没有出现通行密钥引导页
        if any(marker in url_now for marker in moved_on_markers):
            return False
        # URL 跳转可能滞后于 DOM：跳过按钮已出现就直接处理
        try:
            if await page.locator("#cancelSetupSelect_btn:visible").count() > 0:
                return await _skip_passkey_prompt(page)
        except Exception:
            pass
        await asyncio.sleep(0.5)
    return False

async def _wait_for_url_change(page: Page, current_url: str, wait_seconds: int) -> bool:
    """等页面自行跳走。返回 True 表示 URL 已变化。"""
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        await asyncio.sleep(1)
        if page.url != current_url:
            return True
    return False

async def _ensure_hcaptcha_hook(page: Page) -> None:
    """用 page.add_init_script 在所有后续页面加载前注册 hCaptcha 拦截器。

    hCaptcha render=explicit 模式下，Angular 组件在 hCaptchaLoad 回调中调用
    hcaptcha.render(el, {callback: onSuccess})。hcaptcha.render 内部存储回调。
    必须在 hCaptcha API 脚本创建 window.hcaptcha 时拦截，包装 render 方法，
    在回调注册时捕获到 window.__hCaptchaCallback。
    """
    await page.add_init_script(
        r"""(() => {
            // 拦截 hCaptcha API 脚本创建 window.hcaptcha 对象
            let _realHcaptcha = null;
            Object.defineProperty(window, 'hcaptcha', {
                configurable: true,
                enumerable: true,
                get() { return _realHcaptcha; },
                set(val) {
                    _realHcaptcha = val;
                    if (val && typeof val.render === 'function') {
                        const origRender = val.render.bind(val);
                        val.render = function(el, opts) {
                            if (opts && typeof opts.callback === 'function') {
                                window.__hCaptchaCallback = opts.callback;
                            }
                            return origRender(el, opts);
                        };
                    }
                }
            });
        })()"""
    )

async def _print_clickable_snapshot(page: Page) -> None:
    buttons = await page.evaluate(
        r"""() => Array.from(document.querySelectorAll(
            'button, [role="button"], input[type="button"], input[type="submit"]'
        )).slice(0, 20).map((element) => ({
            text: [element.innerText, element.textContent, element.value, element.getAttribute('aria-label')]
                .filter(Boolean).join(' ').replace(/\s+/g, ' ').trim(),
            disabled: Boolean(element.disabled || element.getAttribute('aria-disabled') === 'true'),
            visible: window.getComputedStyle(element).display !== 'none' && element.getClientRects().length > 0
        }))"""
    )
    print("  clickable snapshot:")
    print(json.dumps(buttons, ensure_ascii=False, indent=2))

# ---------------------------------------------------------------------------
#  阶段 C：注册后跳转 + 建 key
# ---------------------------------------------------------------------------

async def finalize_and_create_key(
    page: Page,
    inbox,
    password: str,
    config: AppConfig,
) -> str | None:
    """注册提交后依次处理页面跳转，直到 session 有效并建 key。

    真实跳转链（实测确认）：
      验证码提交 → passkey/prompt-setup 页(点"稍后再说"跳过)
      → signin-redirect → consent 页(点"提交")
      → select-account(填组织名) → complete-profile(session 已有效, 直接建 key)
    """
    print("\n[阶段C] 处理注册后跳转，直到 session 有效...")
    deadline = time.monotonic() + 120
    last_url = ""
    ngc_login_attempts = 0

    while time.monotonic() < deadline:
        # 每轮先尝试直接建 key（session 可能已经有效）
        org_name = await _get_org_name(page)
        if org_name:
            print(f"  session 有效，orgName: {org_name}")
            return await _create_key_in_browser(page, org_name, config)

        url_now = page.url
        if url_now != last_url:
            print(f"  当前页面: {url_now[:90]}")
            last_url = url_now

        # 通行密钥引导页 → 点"稍后再说"跳过。
        # register_account 里已尝试过跳过，此处 URL 可能只是还没来得及跳走，
        # 所以先给它一点时间自行离开，避免重复点击与无意义的告警。
        if "passkey" in url_now:
            if await _wait_for_url_change(page, url_now, wait_seconds=5):
                continue
            await _skip_passkey_prompt(page)
            await asyncio.sleep(3)
            continue

        # 创建组织页（利用组织名跳过手机验证）
        if "select-account" in url_now or "cloudaccounts.nvidia.com" in url_now:
            print("  创建组织页：填组织名...")
            await _create_org(page, config.nvidia.account_name)
            await asyncio.sleep(4)
            continue

        # consent 页 → 点提交
        if "consent" in url_now or "static-login.nvidia.com" in url_now:
            print("  consent 页：点提交...")
            await _click_continue(page)
            await asyncio.sleep(3)
            continue

        if "ngc.nvidia.com/signin" in url_now:
            if ngc_login_attempts >= 2:
                print("  NGC 登录重试耗尽")
                return None
            ngc_login_attempts += 1
            print(f"  NGC session 丢失，重新登录 ({ngc_login_attempts}/2)...")
            if not await _login_ngc(page, inbox.address, password):
                print("  NGC 登录表单未能推进")
                return None
            continue

        if "profile-complete" in url_now:
            try:
                if await page.locator('input[type="number"]:visible').count() >= 6:
                    print("  仍停留在验证码页面，注册未完成")
                    return None
            except Exception:
                pass

        # signin-redirect、complete-profile 等 → 等待跳转
        await asyncio.sleep(2)

    print("  阶段C 超时，未能建 key")
    return None


async def _login_ngc(page: Page, email: str, password: str) -> bool:
    """Advance the NGC login form after registration redirects without a session."""
    original_url = page.url
    submitted_password = False
    for _ in range(4):
        password_input = page.locator('input[type="password"]:visible').first
        try:
            if await password_input.count() > 0 and await password_input.is_visible():
                await password_input.fill(password)
                if not await _click_first_named_button(
                    page,
                    ("Log In", "Sign In", "Continue", "登录", "继续"),
                ):
                    return False
                submitted_password = True
                await asyncio.sleep(3)
                continue
        except Exception:
            pass

        email_input = page.locator(
            'input[type="email"]:visible, input[name="email"]:visible, '
            'input[autocomplete="email"]:visible, input[placeholder*="@"]:visible'
        ).first
        try:
            if await email_input.count() > 0 and await email_input.is_visible():
                await email_input.fill(email)
                if not await _click_first_named_button(
                    page,
                    ("Continue", "Next", "继续", "下一步"),
                ):
                    return False
                await asyncio.sleep(2)
                continue
        except Exception:
            pass

        if page.url != original_url:
            return True
        await asyncio.sleep(1)
    return submitted_password and page.url != original_url


async def _click_first_named_button(page: Page, names: tuple[str, ...]) -> bool:
    for name in names:
        try:
            button = page.get_by_role("button", name=name).first
            if await button.count() > 0 and await button.is_visible() and await button.is_enabled():
                await button.click(timeout=5000)
                return True
        except Exception:
            continue
    return False

async def _get_org_name(page: Page) -> str | None:
    """在浏览器上下文内 fetch user-context（credentials:include），拿 orgName。"""
    try:
        result = await page.evaluate(
            """async () => {
                try {
                    const resp = await fetch('https://api.ngc.nvidia.com/user-context', {
                        credentials: 'include',
                        headers: {'accept': 'application/json'}
                    });
                    if (!resp.ok) return {ok: false, status: resp.status};
                    const data = await resp.json();
                    return {ok: true, orgName: data.orgName || null};
                } catch (e) {
                    return {ok: false, error: String(e)};
                }
            }"""
        )
    except Exception:
        return None
    if result and result.get("ok"):
        return result.get("orgName")
    return None

async def _create_key_in_browser(page: Page, org_name: str, config: AppConfig) -> str | None:
    """在浏览器上下文内 POST 建 key（credentials:include），返回 nvapi-... key。"""
    print("  POST /keys/type/AI_PLAYGROUNDS_KEY...")
    payload = {
        "expiryDate": config.nvidia.key_expiry_date,
        "name": config.nvidia.key_name,
        "type": "AI_PLAYGROUNDS_KEY",
        "policies": [
            {
                "product": "nv-cloud-functions",
                "scopes": ["invoke_function"],
                "resources": [{"id": "*", "type": "account-functions"}],
            }
        ],
    }
    result = await page.evaluate(
        """async ({orgName, payload}) => {
            try {
                const resp = await fetch(
                    `https://api.ngc.nvidia.com/v3/orgs/${orgName}/keys/type/AI_PLAYGROUNDS_KEY`,
                    {
                        method: 'POST',
                        credentials: 'include',
                        headers: {'content-type': 'application/json', 'accept': '*/*'},
                        body: JSON.stringify(payload)
                    }
                );
                const text = await resp.text();
                let data = null;
                try { data = JSON.parse(text); } catch (_) {}
                return {status: resp.status, data, text: text.slice(0, 300)};
            } catch (e) {
                return {status: 0, error: String(e)};
            }
        }""",
        {"orgName": org_name, "payload": payload},
    )

    status = result.get("status")
    if status not in (200, 201):
        print(f"  建 key 失败: {status}: {result.get('text') or result.get('error')}")
        return None

    data = result.get("data") or {}
    api_key = (
        (data.get("apiKey") or {}).get("value", "")
        or (data.get("result") or {}).get("apiKey", {}).get("value", "")
    )
    if api_key:
        print(f"\nAI_PLAYGROUNDS_KEY: {api_key}")
        return api_key
    print("  响应中未找到 apiKey.value")
    return None

async def _create_org(page: Page, org_name: str) -> bool:
    """在 select-account 页填组织名并创建（跳过手机验证的关键）。"""
    text_input = page.locator('input[type="text"]:visible').first
    if await text_input.count() == 0:
        return False
    await text_input.click()
    await text_input.fill(org_name)
    await asyncio.sleep(0.5)

    btn = page.get_by_role("button", name="Create NVIDIA Cloud Account").first
    try:
        await btn.wait_for(state="visible", timeout=5000)
        for _ in range(10):
            if await btn.is_enabled():
                await btn.click()
                print("  clicked [Create NVIDIA Cloud Account]")
                return True
            await asyncio.sleep(0.5)
    except Exception:
        pass
    return False

async def _close_browser(browser, delay: int) -> None:
    print(f"\nBrowser will close in {delay} seconds...")
    await asyncio.sleep(delay)
    await browser.close()

if __name__ == "__main__":
    main_cli()
