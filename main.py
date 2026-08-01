#!/usr/bin/env python3
"""
nvidia-register — 注册 build.nvidia.com 账号并创建 AI_PLAYGROUNDS_KEY

完整流程（基于真实页面链路，全部实测确认）：
  创建临时邮箱 → build.nvidia.com 填邮箱 → create-account 页填密码 + 过 hCaptcha
  → 验证码页真实键盘输入 → 同意/快完成页 → (session 丢失) 邮箱+密码重新登录
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

# ---------------------------------------------------------------------------
#  CLI
# ---------------------------------------------------------------------------

def _parse_count(argv: list[str]) -> int | None:
    """从命令行参数解析 -n / --count 的值。"""
    args = argv[1:]
    i = 0
    while i < len(args):
        if args[i] in ("-n", "--count") and i + 1 < len(args):
            try:
                return int(args[i + 1])
            except ValueError:
                print(f"Error: {args[i]} requires a number, got: {args[i + 1]}")
                sys.exit(1)
        i += 1
    return None

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

    count = _parse_count(sys.argv)
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
    email_provider = build_email_provider(config)
    captcha_solver = build_captcha_solver(config.captcha)

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

    success_count = 0
    fail_count = 0

    async with async_playwright() as p:
        for i in range(count):
            if _shutdown:
                break

            print(f"\n{'#' * 60}")
            print(f"# 账号 {i + 1} / {count}")
            print(f"{'#' * 60}")

            api_key = await _register_one(p, config, email_provider, captcha_solver)
            if api_key:
                success_count += 1
            else:
                fail_count += 1

            # 非最后一个账号时，间隔一下避免频率限制
            if i < count - 1 and not _shutdown:
                print("\n  等待 5 秒后注册下一个...")
                await asyncio.sleep(5)

    # 汇总
    print("\n" + "=" * 60)
    print(f"完成! 成功: {success_count}, 失败: {fail_count}, 总计: {success_count + fail_count}")
    print("=" * 60)

async def _register_one(
    p,
    config: AppConfig,
    email_provider: TempEmailProvider,
    captcha_solver,
) -> str | None:
    """单个账号的完整注册流程。返回 api_key 或 None。"""
    password = generate_password(12)
    reset_captcha_state()  # 重置 sitekey 缓存，确保每个账号独立
    browser = await p.chromium.launch(
        headless=config.browser.headless,
        args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
    )
    page = await browser.new_page(viewport={"width": 1280, "height": 800})

    try:
        # 1. 创建临时邮箱
        inbox_name = "nv" + str(int(time.time()))[-8:]
        try:
            inbox = email_provider.create_inbox(inbox_name)
        except Exception as exc:
            print(f"  Email creation failed: {exc}")
            return None
        print(f"\n[1] Email: {inbox.address}")

        # 2. 打开 build.nvidia.com，接受 cookie 弹窗
        print("[2] Opening build.nvidia.com...")
        await page.goto("https://build.nvidia.com/", wait_until="domcontentloaded", timeout=90000)
        await _accept_cookie_banner(page)

        # 3. 点击 Login 打开登录弹窗
        print("[3] Open sign-in modal...")
        if not await _open_signin_modal(page):
            print("  Login button not found")
            await _print_clickable_snapshot(page)
            return None

        # 4. 填邮箱 → Next（跳转到 login.nvgs.nvidia.com/v1/create-account）
        print("[4] Submit email...")
        start_capturing_sitekey(page)
        await _ensure_hcaptcha_hook(page)
        if not await _submit_email_step(page, inbox.address):
            print("  Failed at email step")
            await _print_clickable_snapshot(page)
            return None

        # 5. 注册（填密码 → 过 hCaptcha → 提交 → 验证码）
        ok = await register_account(page, inbox, password, email_provider, captcha_solver, config)
        if not ok:
            print("\nRegistration failed")
            return None

        # 6. 状态机处理注册后跳转，直到 session 有效并建 key
        api_key = await finalize_and_create_key(page, inbox, password, config)

        # 7. 记录到 CSV
        if api_key:
            append_account_record(
                path=config.nvidia.output_csv,
                email=inbox.address,
                password=password,
                api_key=api_key,
            )
            print(f"  Record saved to: {config.nvidia.output_csv}")
            print(f"\n  ✓ {inbox.address} → {api_key[:30]}...")
            return api_key
        else:
            print("\nRegistration succeeded but API Key creation failed")
            return None
    finally:
        await _close_browser(browser, config.browser.close_delay_seconds)

# ---------------------------------------------------------------------------
#  子流程
# ---------------------------------------------------------------------------

async def _accept_cookie_banner(page: Page) -> None:
    """OneTrust cookie 弹窗会用遮罩拦截点击，必须先接受。"""
    try:
        btn = page.locator("#onetrust-accept-btn-handler")
        await btn.wait_for(state="visible", timeout=8000)
        await btn.click()
        print("  cookie accepted")
        await page.locator("div.onetrust-pc-dark-filter").wait_for(state="hidden", timeout=5000)
    except Exception:
        pass
    await asyncio.sleep(1)

async def _open_signin_modal(page: Page) -> bool:
    """点击 header 的 Login，打开 signin 弹窗。

    实测：点 Login 后先出现临时弹窗，1~2 秒后页面自动刷新出真正有效弹窗。
    """
    try:
        login = page.get_by_role("button", name="Login").first
        await login.wait_for(state="visible", timeout=10000)
        await login.click()
    except Exception:
        pass

    # 等待邮箱输入框首次出现（第一个临时弹窗）
    try:
        await page.locator('input[name="email"]').first.wait_for(state="visible", timeout=8000)
    except Exception:
        pass

    # 关键：等页面自动刷新出第二个有效弹窗。刷新会重建 DOM，
    # 等 email 输入框稳定（连续多次拿到同一个可交互输入框）后再返回。
    await _wait_for_stable_email_input(page)
    return await page.locator('input[name="email"]:visible').count() > 0

async def _wait_for_stable_email_input(page: Page, settle_seconds: float = 3.0) -> None:
    """等待 signin 弹窗自动刷新完成，直到可见 email 输入框稳定。"""
    deadline = time.time() + 15
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

    try:
        await page.wait_for_url("**/login.nvgs.nvidia.com/**", timeout=20000)
        print(f"  navigated to: {page.url[:80]}")
    except Exception:
        await asyncio.sleep(5)
    return True

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
        await page.locator("#registration_password").wait_for(state="visible", timeout=30000)
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
        known_message_ids = email_provider.snapshot_message_ids(inbox)
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
    code = email_provider.poll_verification_code(
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

    # 点“继续”提交验证码
    await _click_continue(page)
    await asyncio.sleep(3)
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
      验证码提交 → signin-redirect → consent 页(点"提交")
      → select-account(填组织名) → complete-profile(session 已有效, 直接建 key)
    """
    print("\n[阶段C] 处理注册后跳转，直到 session 有效...")
    deadline = time.time() + 240
    last_url = ""

    while time.time() < deadline:
        # 每轮先尝试直接建 key（session 可能已经有效）
        org_name = await _get_org_name(page)
        if org_name:
            print(f"  session 有效，orgName: {org_name}")
            return await _create_key_in_browser(page, org_name, config)

        url_now = page.url
        if url_now != last_url:
            print(f"  当前页面: {url_now[:90]}")
            last_url = url_now

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

        # signin-redirect、complete-profile 等 → 等待跳转
        await asyncio.sleep(2)

    print("  阶段C 超时，未能建 key")
    return None

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
