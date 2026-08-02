# nvidia-register

自动注册 NVIDIA BUILD 账号并创建 api key

> 这是基于上游 [zseek/nvidia-register](https://github.com/zseek/nvidia-register) 的非官方魔改版。
> 我们保留上游的注册、临时邮箱和 API Key 流程，并加入了独立、无需第三方验证码平台的视觉
> LLM 方案。感谢上游作者维护原始项目；如果你只需要原版流程，建议直接使用并支持上游项目。

## 功能特点

- **全自动流程**：创建临时邮箱 → 注册 → 过验证码 → 创建组织 → 建 Key → 记录 CSV，全流程自动化
- **批量注册**：支持单次注册多个账号，交互式询问或 `-n` 参数直接指定
- **验证码**：支持手动模式（`manual`）、YesCaptcha（`yescaptcha`）、CaptchaRun（`captcharun`）和视觉模型（`llm`）
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
- （推荐）当前配置中的视觉模型 `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning` 与对应 API Key
- （可选）其他支持图像输入和 JSON Schema 输出的 Responses API 或 Chat Completions 服务

## 安装

```bash
# 推荐：使用 uv 安装运行时和开发依赖
uv sync
uv run playwright install chromium

# 或使用 pip
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
mode = "llm" # manual | yescaptcha | captcharun | llm
yescaptcha_client_key = ""
yescaptcha_api_url = "https://api.yescaptcha.com"
captcharun_token = ""
captcharun_api_url = "https://api.captcha-run.com"
llm_model = "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning"
llm_api_protocol = "responses" # responses | chat_completions
llm_api_base = "https://your-llm-provider.example/v1"
llm_api_key = "your_llm_api_key"
llm_reasoning_effort = "none" # auto | none | minimal | low | medium | high | xhigh | max
llm_call_delay_seconds = 1
llm_action_delay_seconds = 1
llm_calls_per_attempt = 8
llm_max_attempts = 2
llm_max_output_tokens = 1200
llm_max_concurrency = 1
llm_artifact_dir = "failure_artifacts/llm" # optional; keeps bounded failure screenshots and trace.jsonl
poll_interval_seconds = 3
timeout_seconds = 240

[nvidia]
output_csv = "accounts.csv"
key_name = "api"
account_name = "NVIDIA Build"
key_expiry_date = "2126-05-08T08:00:00Z"

[browser]
headless = false
concurrency = 1
launch_stagger_seconds = 8
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
| `captcha.llm_api_protocol` | LLM 协议：`responses`（当前配置）或 `chat_completions`（其他兼容服务） |
| `captcha.llm_api_base` | 对应协议的 API Base；程序会自动补全 `/chat/completions` 或 `/responses` |
| `captcha.llm_api_key` | LLM API Key（mode=llm 时必填）；密钥只放在本地 `config.toml`，不要提交 |
| `captcha.llm_reasoning_effort` | 推理强度；`auto` 表示不发送 `reasoning` 参数 |
| `captcha.llm_call_delay_seconds` | 截图和相邻模型调用前的页面等待时间（秒） |
| `captcha.llm_action_delay_seconds` | 执行动作后、截取新画面前的等待时间（秒） |
| `captcha.llm_calls_per_attempt` | 每轮最多调用模型的次数（1-50） |
| `captcha.llm_max_attempts` | hCaptcha 重置后的最大轮数（1-10） |
| `captcha.llm_max_output_tokens` | 单次模型调用的最大输出 token（128-32768） |
| `captcha.llm_max_concurrency` | LLM API 最大并发请求数（1-10，默认 1；独立于浏览器并发） |
| `captcha.llm_artifact_dir` | 可选失败诊断目录；截图去重，成功会话自动删除，仅保留最近 20 个失败/超时会话 |
| `captcha.poll_interval_seconds` | 验证码结果轮询间隔（秒） |
| `captcha.timeout_seconds` | 验证码等待超时时间（秒） |
| `nvidia.output_csv` | 记录输出 CSV 文件路径 |
| `nvidia.key_name` | API Key 名称 |
| `nvidia.account_name` | 创建组织账户时填入的名称（用于跳过手机验证） |
| `nvidia.key_expiry_date` | API Key 过期时间（默认 ~100 年） |
| `browser.headless` | 是否无头模式运行浏览器 |
| `browser.concurrency` | 隔离浏览器会话的最大并发数（1-10，默认 1） |
| `browser.launch_stagger_seconds` | 相邻浏览器会话启动的最小间隔（0-300 秒，默认 8） |
| `browser.close_delay_seconds` | 完成后浏览器关闭延迟秒数 |

### 当前 LLM 配置

本分支按本地 `config.toml` 使用 **`nvidia/nemotron-3-nano-omni-30b-a3b-reasoning`**，通过
`https://your-llm-provider.example/v1` 的 Responses API 完成截图识别和结构化动作输出。这里的“自给自足”指
本项目新增了独立的 LLM 识图验证码路径，不再强制依赖 YesCaptcha 或 CaptchaRun；接口密钥仍由使用者
自行提供，且只保存在被 `.gitignore` 忽略的 `config.toml` 中。

如果更换服务，必须同时确认模型支持视觉输入、严格 JSON Schema，以及配置的 API 协议；仅支持文本
对话的模型不能用于 `llm` 模式。

## 使用

```bash
# 交互式询问注册数量
python main.py

# 直接指定注册数量（不询问）
python main.py -n 5
python main.py --count 3

# 无头浏览器 + LLM 视觉识图（覆盖 browser.headless 配置）
python main.py --headless -n 1

# 同时运行 3 个相互隔离的无头会话，共注册 10 个账号
python main.py --headless -n 10 -j 3

# 临时切回有头浏览器排障
python main.py --headed -n 1
```

批量注册使用固定大小的 worker 池，每个账号拥有独立浏览器会话。`Ctrl+C` 后不再领取新账号，
当前并发中的账号完成后显示成功/失败汇总。
`--headless`、`--headed` 和 `-j/--concurrency` 只覆盖本次运行，不修改 `config.toml`。无头模式必须配合
`llm`、`yescaptcha` 或 `captcharun` 自动验证码模式；`manual` 无法显示挑战，程序会在启动时
直接拒绝该组合。

每个并发任务使用独立的 Browser、Context、Page、临时邮箱和验证码 solver；hCaptcha sitekey
也按 Page 隔离。同步邮箱轮询在独立线程运行，CSV 记录通过锁串行追加，因此一个会话等待
邮件或识图时不会阻塞或污染其他会话。

浏览器并发和 LLM API 并发分别控制。多个验证码会话可以同时推进，但只有实际的模型 HTTP
请求受 `llm_max_concurrency` 限制；默认串行请求，以减少兼容接口的 503、TLS 中断和限流错误，
不会再让一个验证码独占请求槽直到整场结束。失败账号会在 `failure_artifacts/` 下保存阶段、URL 和页面
截图；其中可能包含注册邮箱，排障完成后应删除。

每次注册成功会自动追加记录到 `accounts.csv`：

```csv
email,password,apikey
nv12345678@your-domain.com,aB3dE5fG7hI9,nvapi-xxxx...
```

## LLM 验证码模式

将 `captcha.mode` 设置为 `llm` 后，程序优先从 hCaptcha canvas 导出原生分辨率 PNG，
同时从 DOM 读取挑战题目，再发送到配置的 LLM API。`chat_completions` 模式使用
`/chat/completions` 的视觉输入和 JSON Schema；`responses` 模式使用 `/responses` 的
`input_image` 和 JSON Schema。canvas 无法导出时才退回 iframe/视口截图。其他验证码模式仍可
通过原有选项选择。

本分支实际使用的模型就是 **`nvidia/nemotron-3-nano-omni-30b-a3b-reasoning`**。模型选择不是
硬编码限制，配置兼容的视觉模型即可替换。

LLM 模式可在无头 Chromium 中运行，推荐使用 `python main.py --headless -n 1`。浏览器截图、
canvas 导出和鼠标坐标映射都由 Playwright 页面对象完成，不依赖桌面显示服务。

默认相邻截图/动作等待 1 秒，每轮最多调用 8 次，最多执行 2 轮；一轮失败后会尝试重置
hCaptcha。普通题每次模型调用最多执行一个点击或拖动；3×3 多选题允许一次返回最多 9 个
格子，控制器逐格执行后自动点击 hCaptcha 的 `Verify`/`Check`。checkbox 小帧只由 DOM 操作，
不会发送给模型，避免 iframe 切换后继续使用旧坐标。模型请求遇到网络异常、429 或 5xx 时，
会在同一个 30 秒总预算内进行至多一次瞬时重试。

对于固定 4×4 的动物补位题，程序会先在原生 canvas 上比较每行图标、空格和两个候选的
像素差。只有异常行、空格和候选匹配三项置信度都达到阈值时，才使用网格中心坐标执行；
否则仍交给视觉模型。这可避免模型把已占用格误报为空格，其他点击和轮廓拖拽题仍由 LLM
决策。

模型必须返回以下结构：

```json
{
  "status": "actions",
  "actions": [
    {
      "kind": "click",
      "start_x": 190,
      "start_y": 323,
      "end_x": null,
      "end_y": null,
      "grid_row": 1,
      "grid_column": 1
    }
  ],
  "message": "",
  "coordinate_space": "normalized_1000"
}
```

`status` 支持 `actions`、`verify` 和 `solved`（解析器仍兼容旧的 `failed` 响应）。坐标固定
使用 0-1000 归一化空间；3×3 DOM 网格还必须给出从 1 开始的 `grid_row/grid_column`，控制器
优先按行列落到格子中心，其他任务将这两个字段设为 `null`。原生 canvas 坐标会按它的 CSS
显示比例映射回页面，超出截图范围的动作会被拒绝。配置的 API 必须支持对应协议的图片输入和
严格 JSON Schema 输出。
第三方兼容接口即使返回 Markdown 包裹的 JSON 也会尝试提取；若配置了
`llm_artifact_dir`，其中会记录原始响应、canvas/CSS 缩放、局部坐标和最终页面坐标。相同截图
只写一次，每个失败会话最多保留 16 张常规截图，并仅保留最近 20 个失败/超时会话；成功会话
立即删除，超过 10 分钟且已超过两倍验证码超时的异常中断目录也会纳入清理。回退截图可能
包含当前注册页面信息，排障完成后仍应删除不再需要的目录。

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

## 测试

```bash
uv run pytest -q
```
