# nvidia-register

自动注册 NVIDIA BUILD 账号并创建 api key

## 功能特点

- **全自动流程**：创建临时邮箱 → 注册 → 过验证码 → 创建组织 → 建 Key → 记录 CSV，全流程自动化
- **批量注册**：支持单次注册多个账号，交互式询问或 `-n` 参数直接指定
- **验证码**：支持手动模式（`manual`）以及 YesCaptcha（`yescaptcha`）、CaptchaRun（`captcharun`）、视觉模型（`llm`）三种自动模式
- **邮箱服务**：支持 `cloudflare_temp_email`（自部署）和 `duckmail`（DuckMail API）
- **随机密码**：每次注册自动生成 12 位密码（大小写 + 数字）
- **自动跳过手机验证**：利用组织名注册跳过手机号要求，并创建长效 API Key
- **CSV 记录**：每次注册成功立即追加 `email,password,apikey` 到 CSV 文件
- **优雅退出**：`Ctrl+C` 完成当前账号后安全退出

## 项目结构

```
├── main.py              # 主入口 + 流程编排
├── config.py            # 配置加载（config.toml）
├── email_providers.py   # 临时邮箱服务抽象层
├── captcha.py           # 验证码处理
├── passwords.py         # 随机密码生成
├── records.py           # CSV 记录写入
├── config.toml          # 配置文件
└── config.toml.example  # 配置示例
```

## 前置条件

- Python 3.11+
- Chromium 浏览器（Playwright 自动下载）
- **临时邮箱服务**（当前支持 `cloudflare_temp_email` 自部署 和 `duckmail`）
- （可选）[YesCaptcha](https://yescaptcha.com/i/57yzUt) / [CaptchaRun](https://captcha-run.com/sso?inviter=ad8fbc2f-9721-430e-87a9-1898fa0177b4) 密钥（用于对应的自动模式）
- （可选）支持图像输入和 JSON Schema 输出的 Responses API 模型与 API Key（用于 `llm` 模式）

## 安装

```bash
pip install -r requirements.txt
playwright install chromium
```

## 配置

```bash
# 生成配置文件模板
python main.py --init
```

编辑生成的 `config.toml`：

```toml
email_provider = "cloudflare_temp_email"

[cloudflare_temp_email]
api_url = "https://mail.your-server.com"
admin_auth = "your_admin_key"
custom_auth = ""
domain = "your-domain.com"

[duckmail]
api_url = "https://api.duckmail.sbs"
domain = "duckmail.sbs"
api_key = ""

[captcha]
mode = "manual" # manual | yescaptcha | captcharun | llm
yescaptcha_client_key = ""
yescaptcha_api_url = "https://api.yescaptcha.com"
captcharun_token = ""
captcharun_api_url = "https://api.captcha-run.com"
llm_model = ""
llm_api_base = "https://api.openai.com/v1"
llm_api_key = ""
llm_reasoning_effort = "auto" # auto | none | minimal | low | medium | high | xhigh | max
llm_call_delay_seconds = 5
llm_action_delay_seconds = 5
llm_calls_per_attempt = 10
llm_max_attempts = 2
llm_max_output_tokens = 1200
llm_artifact_dir = "" # optional; saves screenshots and trace.jsonl
poll_interval_seconds = 3
timeout_seconds = 180

[nvidia]
output_csv = "accounts.csv"
key_name = "api"
account_name = "NVIDIA Build"
key_expiry_date = "2126-05-08T08:00:00Z"

[browser]
headless = false
close_delay_seconds = 5
```

| 配置项 | 说明 |
|--------|------|
| `email_provider` | 临时邮箱服务类型（支持 `cloudflare_temp_email` / `duckmail`） |
| `cloudflare_temp_email.api_url` | 邮箱服务 API 地址 |
| `cloudflare_temp_email.admin_auth` | 邮箱服务管理员密钥 |
| `cloudflare_temp_email.custom_auth` | 站点访问密码（网站启用私人访问密码时需填写，对应 `x-custom-auth`） |
| `cloudflare_temp_email.domain` | 邮箱域名 |
| `duckmail.api_url` | DuckMail API 地址（默认 `https://api.duckmail.sbs`） |
| `duckmail.domain` | DuckMail 邮箱域名，例如 `duckmail.sbs` 或你的私有域名 |
| `duckmail.api_key` | DuckMail 私有域 API Key，使用公共域名时可留空 |
| `captcha.mode` | 验证方式：`manual`、`yescaptcha`、`captcharun` 或 `llm`；新增的 `llm` 是可选模式，不改变其他模式 |
| `captcha.yescaptcha_client_key` | YesCaptcha 客户端密钥（mode=yescaptcha 时必填） |
| `captcha.yescaptcha_api_url` | YesCaptcha API 地址（默认 `https://api.yescaptcha.com`） |
| `captcha.captcharun_token` | CaptchaRun Authorization Token（mode=captcharun 时必填） |
| `captcha.captcharun_api_url` | CaptchaRun API 地址（默认 `https://api.captcha-run.com`） |
| `captcha.llm_model` | 支持视觉输入的模型名称（mode=llm 时必填） |
| `captcha.llm_api_base` | Responses API Base 或完整 `/responses` 地址 |
| `captcha.llm_api_key` | Responses API Key（mode=llm 时必填） |
| `captcha.llm_reasoning_effort` | 推理强度；`auto` 表示不发送 `reasoning` 参数 |
| `captcha.llm_call_delay_seconds` | 截图和相邻模型调用前的页面等待时间（秒） |
| `captcha.llm_action_delay_seconds` | 执行动作后、截取新画面前的等待时间（秒） |
| `captcha.llm_calls_per_attempt` | 每轮最多调用模型的次数（1-50） |
| `captcha.llm_max_attempts` | hCaptcha 重置后的最大轮数（1-10） |
| `captcha.llm_max_output_tokens` | 单次模型调用的最大输出 token（128-32768） |
| `captcha.llm_artifact_dir` | 可选调试目录，保存模型实际看到的截图和 `trace.jsonl`；默认关闭 |
| `captcha.poll_interval_seconds` | 验证码结果轮询间隔（秒） |
| `captcha.timeout_seconds` | 验证码等待超时时间（秒） |
| `nvidia.output_csv` | 记录输出 CSV 文件路径 |
| `nvidia.key_name` | API Key 名称 |
| `nvidia.account_name` | 创建组织账户时填入的名称（用于跳过手机验证） |
| `nvidia.key_expiry_date` | API Key 过期时间（默认 ~100 年） |
| `browser.headless` | 是否无头模式运行浏览器 |
| `browser.close_delay_seconds` | 完成后浏览器关闭延迟秒数 |

## 使用

```bash
# 交互式询问注册数量
python main.py

# 直接指定注册数量（不询问）
python main.py -n 5
python main.py --count 3
```

批量注册时每个账号使用独立的浏览器会话，间隔 5 秒。`Ctrl+C` 优雅退出：完成当前正在注册的账号后停止，显示成功/失败汇总。

每次注册成功会自动追加记录到 `accounts.csv`：

```csv
email,password,apikey
nv12345678@your-domain.com,aB3dE5fG7hI9,nvapi-xxxx...
```

## LLM 验证码模式

将 `captcha.mode` 显式设置为 `llm` 后，程序优先从 hCaptcha canvas 导出原生分辨率 PNG，
同时从 DOM 读取挑战题目，再发送到 Responses API。canvas 无法导出时才退回 iframe/视口
截图。其他验证码模式仍可通过原有选项选择。

默认首次截图前等待 5 秒，每轮最多调用 10 次，最多执行 2 轮；一轮失败后会尝试重置
hCaptcha。每次模型调用最多执行一个点击或拖动，页面更新后重新定位 hCaptcha iframe 并
截图，避免继续使用过期画面中的坐标。模型返回 `verify` 后，程序通过 DOM 点击 hCaptcha
自己的 `Verify`/`Check` 控件；拖拽题会在一次拖拽后直接尝试该控件。

对于固定 4×4 的动物补位题，程序会先在原生 canvas 上比较每行图标、空格和两个候选的
像素差。只有异常行、空格和候选匹配三项置信度都达到阈值时，才使用网格中心坐标执行；
否则仍交给视觉模型。这可避免模型把已占用格误报为空格，其他点击和轮廓拖拽题仍由 LLM
决策。

模型必须返回以下结构：

```json
{
  "status": "actions",
  "actions": [
    {"kind": "click", "start_x": 120, "start_y": 80, "end_x": null, "end_y": null}
  ],
  "message": "",
  "coordinate_space": "normalized_1000"
}
```

`status` 支持 `actions`、`verify` 和 `solved`（解析器仍兼容旧的 `failed` 响应）。坐标固定
使用 0-1000 归一化空间；原生 canvas 坐标会按它的 CSS 显示比例映射回页面，超出截图
范围的动作会被拒绝。API 需要兼容 Responses 的 `input_image` 和严格 JSON Schema 输出。
第三方兼容接口即使返回 Markdown 包裹的 JSON 也会尝试提取；若配置了
`llm_artifact_dir`，其中会记录原始响应、canvas/CSS 缩放、局部坐标和最终页面坐标。回退
截图可能包含当前注册页面信息，排障完成后应删除该目录。

## 注册流程

```
build.nvidia.com (填邮箱) → login.nvgs.nvidia.com (填密码 + hCaptcha)
→ 验证码页 (键盘输入) → 同意页 (提交) → 创建组织 (跳过手机验证)
→ NGC API (建 Key) → CSV 记录
```

## 扩展邮箱服务

当前已支持 `cloudflare_temp_email` 和 `duckmail`，后续仍可通过实现 `TempEmailProvider` 协议扩展：

```python
class TempEmailProvider(Protocol):
    def create_inbox(self, name: str) -> TempEmailInbox: ...
    def snapshot_message_ids(self, inbox: TempEmailInbox) -> set[str]: ...
    def poll_verification_code(
        self,
        inbox: TempEmailInbox,
        timeout_seconds: int = 180,
        known_message_ids: set[str] | None = None,
    ) -> str | None: ...
```

在 `email_providers.py` 中添加新 Provider 并注册到 `build_email_provider()` 即可。

## 注意事项

- hCaptcha **手动模式**必须人工完成验证
- 注册包含验证码轮询（最长 3 分钟）
- 浏览器窗口会在完成后自动关闭（可配置延迟）
- 批量注册时每个账号独立浏览器会话，互不影响
- 第二次 `Ctrl+C` 强制退出
