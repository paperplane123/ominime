# OmniMe

> 本地记录你在 macOS 各个应用中实际提交的文字，按天汇总，帮助你回顾工作与思考轨迹。

![Python](https://img.shields.io/badge/Python-3.10+-blue.svg)
![Platform](https://img.shields.io/badge/Platform-macOS-lightgrey.svg)
![License](https://img.shields.io/badge/License-MIT-green.svg)

OmniMe 以一次按下 Enter 的提交为记录边界。它不会把连续的原始按键流逐个写入数据库，而是尽量读取输入框中真正提交的文字，再按应用、时间和会话组织成可复盘的数据。

## 主要功能

- **提交级输入记录**：在文本输入框中按 Enter 时保存本次提交，不记录普通按键流。
- **中英文与输入法处理**：优先记录最终提交文本，过滤输入法预编辑过程，支持中英文混合内容。
- **Kim / 微信发送后确认**：Kim 与微信的 Enter 原样交给应用；消息出现后，OmniMe 才通过受约束的本地 AX 或 Vision 读取最终发送气泡。无法证明气泡属于本次发送时直接跳过，不回退保存输入区草稿或聊天上下文。
- **可信输入保护**：焦点不在文本输入控件、安全输入框、编辑器换行和异常大的整篇内容都会跳过，避免误记网页正文、聊天历史、文档或密码。
- **不可读输入降级**：无法安全读取原文时，可以只保存字符数，不保存拼音或不可信内容。
- **应用与业务日统计**：按应用、会话、小时和日期查看输入量；业务日时区可配置。
- **菜单栏状态**：自动开始或暂停记录，显示本次运行期间的实时输入量，并可直接打开数据目录和 Web 后台。
- **Web 复盘**：查看概览、应用分布、提交内容、上下文、主题分析与工作路径。
- **报告与导出**：支持终端报告、JSON 导出和 Obsidian Markdown 日报。
- **本地优先**：SQLite 数据默认只保存在本机；AI 分析为可选功能。
- **捕获诊断**：记录最近一次提交为何被保存、仅计数或跳过，方便排查漏记与误记。

> OmniMe 依赖 macOS 辅助功能接口。不同应用对输入框内容的开放程度不同，因此它会优先保证“不误记”，无法确认内容可信时可能只计数或跳过。

## 快速开始

### 运行要求

- macOS
- Git
- Python 3.10 或更高版本
- macOS「辅助功能」权限

可以先检查：

```bash
git --version
python3 --version
```

如果 Git 不存在，可先运行 `xcode-select --install` 安装 Apple 命令行工具；如果 Python 低于 3.10，可从 [python.org](https://www.python.org/downloads/macos/) 安装新版 Python，或在已安装 Homebrew 时运行 `brew install python`。

### 推荐安装

```bash
git clone https://github.com/itsoso/ominime.git
cd ominime
chmod +x scripts/install_app.sh
./scripts/install_app.sh
```

已经配置 GitHub SSH Key 的用户也可以使用 `git@github.com:itsoso/ominime.git`。

安装脚本会：

1. 选择合适的原生架构 Python 并创建 `venv`。
2. 安装 OmniMe 及运行依赖。
3. 创建并加载 LaunchAgent，设置登录后自动启动。
4. 启动菜单栏应用。

如果网络环境需要国内镜像，可以这样运行：

```bash
USE_MIRROR=1 ./scripts/install_app.sh
```

### 授予辅助功能权限

第一次启动后，前往：

`系统设置 → 隐私与安全性 → 辅助功能`

推荐安装方式通过项目虚拟环境中的 Python 运行 OmniMe，因此优先授权项目里的 `venv/bin/python`。列表中没有该程序时，点击 `+` 手动添加；如果你从终端直接启动 OmniMe，也按系统提示授权所使用的终端。授权后，在菜单栏中点击 `▶️ 开始记录`。

可以用 LaunchAgent 可靠地重启推荐安装的应用：

```bash
launchctl kickstart -k "gui/$(id -u)/com.ominime.app"
```

状态栏显示：

- `⌨️ 数字`：正在记录，数字是本次运行期间实际保存的字符数，并在业务日切换时归零；被安全规则跳过的按键估算不会混入。
- `⌨️ ⏸`：记录已暂停。
- `⌨️ ⚠`：缺少辅助功能权限。

### 手动安装

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
pip install -e .
ominime
```

不带子命令运行 `ominime`，等同于 `ominime app`。

推荐安装不会把 `ominime` 写入系统 PATH。以后在项目目录执行命令前，可以先激活虚拟环境：

```bash
source venv/bin/activate
```

也可以始终使用完整的项目内命令，例如 `./venv/bin/ominime report`。

## 日常使用

### 菜单栏应用

```bash
ominime
# 或
ominime app
```

菜单栏应用启动后会自动尝试开始记录，并在本机启动 Web 后台。菜单中可以：

- 查看今日统计。
- 打开 Web 后台。
- 开始或暂停记录。
- 查看配置和数据目录。
- 设置或取消登录后自动启动。

### Web 后台

菜单栏应用运行时，打开：

<http://127.0.0.1:8001>

也可以单独启动 Web 服务：

```bash
ominime web
ominime web -p 3000
ominime web --reload
```

默认只监听 `127.0.0.1:8001`。交互式 API 文档位于 <http://127.0.0.1:8001/docs>。

使用 `-p 3000` 等自定义端口时，下文所有 Web、健康检查和诊断地址也要把 `8001` 替换为该端口。

### 报告、统计与导出

```bash
# 今日报告
ominime report

# 指定业务日
ominime report -d 2026-08-05

# 最近 7 天中有记录的日期
ominime stats

# 导出今日数据为 JSON
ominime export

# 导出指定日期和文件
ominime export -d 2026-08-05 -o report.json

# 命令行实时监控
ominime monitor
```

未指定 `-o` 时，JSON 会写入当前目录，文件名形如 `ominime_export_2026-08-05.json`。

### 导出到 Obsidian

建议显式传入自己的 Obsidian vault 路径：

```bash
ominime obsidian -p /path/to/your/vault
ominime obsidian -d 2026-08-05 -p /path/to/your/vault

# 不包含按应用分组的原始提交内容和 AI 分析
ominime obsidian -p /path/to/your/vault --no-raw --no-ai
```

导出文件会写入 vault 的 `10_Sources/OmniMe/` 目录。常用选项：

- `--no-raw`：不在日报中包含原始输入内容。
- `--no-ai`：不生成 AI 分析。
- 环境变量 `OBSIDIAN_PATH`：设置默认 vault 路径。

使用 `--no-raw --no-ai` 后，日报仍会包含日期、应用名、字符数、时间分布、常规总结和工作路径等统计信息，但不会包含按应用分组的原始提交正文或 AI 分析。

## OmniMe 会记录什么

默认 `enter-text` 模式下，OmniMe 会在文本输入控件中发生普通 Enter 提交时尝试保存完整内容。它会从当前输入框和同一字段的近期输入中选择可信内容，尽量只保留输入法最终提交的文字；同一个物理 Enter 和短时间内的相同提交都会去重。

下列情况可能只记录字符数或直接跳过：

- 当前焦点不是文本输入框。
- 焦点是密码等安全输入框。
- 在 TextEdit、备忘录、Obsidian、Word、VS Code 等编辑器的多行正文中按 Enter；此时按键被视为换行。
- 浏览器中的普通多行编辑区没有明确的聊天、提示词或搜索语义；此时默认按换行处理。
- 使用 `Shift+Enter` 或 `Option+Enter` 插入换行。
- 输入框没有向 macOS 辅助功能接口开放文字，且无法确认其他备用内容可信。
- 读取到的内容异常大，可能来自整篇文档、整页或聊天历史。
- 使用了 Command 组合键，而不是普通 Enter 提交。

位于 `ignored_apps` 中的应用会被直接排除，不保存提交记录。

新提交只保存输入记录和不含窗口、控件层级的捕获诊断，不再保存提交上下文元数据。历史版本创建的 `submission_contexts` 表和既有数据会原样保留，但 Web 不再提供读取入口。

Kim 与微信使用独立的发送后链路：EventTap 只把原始 keyDown/keyUp 放行并投递一个小型事件样本，不复制、抑制或重放 Enter，也不在回调中读取 AX、剪贴板、截图或 OCR。后台 worker 对两类来源分别验证：AX 必须在本次发送的时间范围内提供我方方向、唯一消息 ID，并匹配目标 PID、窗口 ID 与会话指纹；Vision 必须匹配相同的 PID、窗口与会话锚点，并额外满足发送前内存基线变化、非空本地 validation 摘要精确一致，以及连续两次文本与几何位置稳定。微信发送时会重建原生窗口；仅当 PID、窗口尺寸和会话指纹仍一致时，Vision 才把新原生窗口视为同一逻辑窗口，Kim 仍要求原生窗口 ID 严格一致。任一来源自身所需证据缺失都只产生不含正文的失败诊断。粘贴时，事件 worker 在 Cmd-V keyUp 后优先读取一次当前 composer AX 值；若 Kim/微信未暴露该值，只记录不含正文的 pasteboard changeCount，等 Enter 通过 secure 检查且剪贴板仍未变化时才瞬时读取本次字符串作为 validation。任何前后混合输入、超时、剪贴板变化、上下文清理或停止都会使该 fallback 失效，不写数据库或日志。

## 数据与隐私

数据目录为 `~/.ominime/`：

- `ominime.db`：SQLite 输入记录、捕获诊断和统计数据；可能包含历史版本保留的提交上下文记录。
- `config.json`：可选的用户配置文件；不存在时使用代码默认值。
- `runtime-state.json`：菜单栏与独立 Web 进程共享的录制状态和短期心跳，不包含输入内容。
- `ominime.log`：应用标准输出日志。
- `ominime.error.log`：应用错误日志。
- `logs/`：自动导出等附加任务的日志目录。

隐私边界：

- 默认数据只写入本机 SQLite。
- `enter-text` 模式保存可信提交原文和字符数。
- `count-only` 模式不保存提交原文，只保存字符数和不含窗口、控件层级的捕获诊断。
- 无法可信读取原文时，系统会在有足够输入证据时只记录字符数；证据不足或内容可能不安全时仍会跳过。
- 捕获流程不会通过通用 `Cmd+A` / `Cmd+C` 读取输入框，因此不会改动当前选择或把整页复制到剪贴板。
- Kim/微信基线与发送后帧最多在进程内存中短暂存在；图片不序列化、不写数据库、不写日志、不写诊断，任务结束即释放。
- Kim/微信发送后捕获只使用本机 macOS AX、Quartz 与 Vision，不调用远程模型。它不集成、不探测 Chatlog/5030，也不直接读取微信数据库。
- 本地 Ollama 或本地 Qwen 后端可以在本机完成分析。
- 可以随时从菜单栏暂停记录，或通过 `ignored_apps` 排除应用。

> 输入记录可能包含聊天、命令、笔记等敏感内容。OmniMe 的分析后端仅允许使用本机模型。

### Kim / 微信发送后诊断与验收

常见失败码包括：

- `post_send_queue_full`、`capture_expired`、`source_read_timeout`：有界队列、任务期限或本地来源超时；消息发送本身不受影响。
- `baseline_unavailable`、`window_identity_mismatch`、`session_anchor_mismatch`：缺少可信基线，或发送后窗口/会话已变化。
- `ocr_validation_unavailable`、`ocr_validation_mismatch`、`ocr_unstable`：无法用本地输入摘要确认 OCR，或连续采样不稳定。
- `duplicate_message_identity`：结构化来源重复返回已处理的同一消息节点。
- `post_send_target_changed`、`post_send_window_changed`、`post_send_session_changed`：完成时前台应用、窗口或会话已切换，因此拒绝写入。

真实验收时，在 Kim 与微信分别发送唯一的短中文、英文/数字、多行、粘贴、豆包候选、连续两条相同文本和快速连续消息。逐条确认数据库正文完全一致、`fallback_source` 为对应的 `*_postsend_ax` 或 `*_postsend_ocr`、仅有一条记录、没有新增 `submission_contexts` 行或截图文件。再测试发送后立即切换会话、切换应用或最小化窗口：应只有命名失败诊断，不能写入旧消息、对方消息或其他会话内容，且应用仍正常收到每一次 Enter。

## 配置

用户配置位于 `~/.ominime/config.json`。如果文件不存在，可以手动创建；只需要填写想覆盖的字段，其余字段继续使用默认值。

这是 JSON 文件，至少应包含 `{}`。可以先从菜单栏选择“打开数据目录”，再用文本编辑器创建或修改它。

```json
{
  "input_capture_mode": "enter-text",
  "count_unreadable_submissions": true,
  "capture_key_event_text_fallback": true,
  "ignored_apps": [
    "com.apple.loginwindow",
    "com.apple.SecurityAgent"
  ],
  "session_timeout": 300,
  "ai_enabled": false
}
```

常用配置项：

| 配置 | 默认值 | 作用 |
| --- | --- | --- |
| `input_capture_mode` | `enter-text` | 使用 `enter-text` 保存可信原文；使用 `count-only` 不保存原文，只保存字符数和非内容诊断。 |
| `count_unreadable_submissions` | `true` | 输入框不可读时是否降级统计物理输入量。 |
| `capture_key_event_text_fallback` | `true` | 输入框不可读时，是否接受可信的已提交输入事件文本。 |
| `day_timezone` | `Asia/Shanghai` | “今日”、日报和统计使用的业务日时区。 |
| `storage_timezone` | 当前环境时区或 `America/New_York` | 解释数据库无时区时间戳时使用的时区。 |
| `ignored_apps` | 系统登录与安全界面 | 不记录指定 bundle ID 的应用。 |

修改配置后请重启 OmniMe。安装时也可以通过环境变量设置时区：

```bash
OMINIME_DAY_TIMEZONE=Asia/Shanghai \
OMINIME_STORAGE_TIMEZONE=America/New_York \
./scripts/install_app.sh
```

普通用户不需要把时区写入 `config.json`：`day_timezone` 默认是 `Asia/Shanghai`，决定报告归属哪一天；`storage_timezone` 优先使用 `OMINIME_STORAGE_TIMEZONE` 或系统 `TZ`，都没有时回退到 `America/New_York`。只有跨时区使用或迁移旧数据库时通常才需要显式修改。

要排除应用，需要把它的 bundle ID 加入 `ignored_apps`。例如查询 Safari 的 bundle ID：

```bash
osascript -e 'id of app "Safari"'
```

自定义 `ignored_apps` 会替换默认列表，因此建议保留示例中的两个系统安全项，再追加自己的应用。

## 可选 AI 分析

AI 功能用于生成每日总结、主题、工作重点、工作路径和建议，仅支持本地 Ollama 和本地 Qwen。AI 默认关闭；只有显式设置 `AI_ENABLED=true` 才会启用，不开启 AI 不影响输入记录、统计和导出基础数据。

OmniMe 不读取 OpenAI Key，也不提供远程模型后端。阿里云 TokenPlan 同样是远程服务，因此当前版本不接入。

```bash
./scripts/setup_local_llm.sh
python3 scripts/check_llm.py
```

详细说明：

- [LLM 后端配置指南](docs/LLM_BACKENDS.md)
- [本地 LLM 部署指南](docs/LOCAL_LLM_GUIDE.md)

## 运行检查与排障

### 查看当前状态

桌面应用运行时访问：

```bash
curl http://127.0.0.1:8001/api/health
```

返回内容包括记录状态、今日字符数、最近写入、捕获模式、数据库路径，以及最近一次捕获诊断。

`recording_status` 的常见值：

- `recording`：正在记录。
- `paused`：已暂停。
- `permission_missing`：缺少辅助功能权限。
- `starting`：正在启动。
- `error`：监听启动或运行失败，可结合 `last_runtime_error` 和错误日志排查。

查看最近的捕获决策：

```bash
curl 'http://127.0.0.1:8001/api/capture/diagnostics?limit=20'
```

诊断中的常见结果：

- `persist_text`：保存了可信原文。
- `persist_count`：只保存字符数。
- `focused_element_not_text_input`：Enter 发生时焦点不是文本输入控件。
- `degraded_context`：辅助功能状态或焦点读取失败，无法确认内容可信。
- `secure_text_input`：焦点是密码或受保护输入框。
- `editor_newline`：在已知编辑器正文中，Enter 被视为换行。
- `newline_modifier`：使用了 Shift 或 Option 组合键插入换行。
- `shortcut_modifier`：使用了 Command 或 Control 组合键。
- `suspected_whole_document`：读取内容异常大，疑似整篇文档或整页内容。
- `no_trusted_content`：没有找到可信的提交内容。

### Safari 部分输入只记录按键数

Safari 网页输入控件不一定始终提供 `AXValue`。当前实验分支在
`AXValue` 为空时，会尝试 macOS 的 `AXStringForRange` 参数化属性，
但**仅在焦点已验证属于同一非密码文本输入控件、文本长度受限时启用**。
不支持的网页仍可能保存为仅计数，不能从按键数还原原始中文。

检查 Safari 的记录原因：

```bash
curl 'http://127.0.0.1:8001/api/capture/diagnostics?app_bundle_id=com.apple.Safari&limit=50'
```

`diagnostics` 中的 `verified_text_entry`、`ax_value_nonempty`、
`snapshot_available`、`snapshot_same_field`、`ax_range_attempted`、
`ax_range_available` 只记录布尔状态，不包含所输入的文字或网页内容。
`selected_source=ax_range` 表示备用接口成功读取。
`focused_role=null` 在该版本是隐私限制下的预期行为，不能据此认定焦点丢失。
这些字段只对新采集的 Safari 记录生效，旧诊断不会补全。

### 没有记录到内容

1. 确认菜单栏不是 `⌨️ ⏸` 或 `⌨️ ⚠`。
2. 重新检查辅助功能权限，并重启 OmniMe。
3. 确认焦点位于真正的文本输入框中。
4. 查看 `/api/health` 的 `recording_status` 和 `last_capture_diagnostic`。
5. 检查 `~/.ominime/ominime.error.log`。

### 菜单栏图标没有出现或 Web 无法访问

先确认 LaunchAgent 已加载：

```bash
launchctl print "gui/$(id -u)/com.ominime.app"
```

然后重新启动：

```bash
launchctl kickstart -k "gui/$(id -u)/com.ominime.app"
```

仍未出现时，查看 `~/.ominime/ominime.log` 和 `~/.ominime/ominime.error.log`。也可以在项目目录直接运行 `./venv/bin/ominime app`，从终端输出中查看启动错误。

### 卸载

```bash
./scripts/uninstall_app.sh
```

卸载脚本会停止进程并移除 LaunchAgent，然后询问是否删除 `~/.ominime/`。选择删除会永久移除本地记录，请先备份需要保留的数据。

## 开发与验证

```bash
source venv/bin/activate
pip install -e .
python -m pytest -q
```

查看全部 CLI：

```bash
ominime --help
```

## License

MIT License
