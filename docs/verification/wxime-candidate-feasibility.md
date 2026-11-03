# 微信输入法候选窗可行性探测（Part B / B1）

状态：**NOT PROVEN —— 已在本机探测，微信输入法不接入**

本文件是接入决策的先决门记录。只有当候选窗结构被判定为 `PROVEN`，才允许把
微信输入法的 bundle id 加入 `IME_CANDIDATE_PROFILES`
（见 `src/ominime/ime_candidate_capture.py`）。在探测通过前，注册表只保留豆包
（`com.bytedance.inputmethod.doubaoime`），微信输入法链路不接入，豆包行为不变。

## 为什么需要本机探测

- 上游原实现把候选读取写死只认豆包，对日常使用微信输入法的用户完全空转。
- 微信输入法候选窗的 AX 结构、进程形态、候选窗是否会锚定在目标 app 输入区，
  均未经证实，不能凭猜测接入。
- 探测脚本只输出**结构布尔/计数**，绝不输出候选文字内容（与项目隐私底线一致）。

## 探测步骤（用户执行）

1. 在系统「输入法」里把微信输入法切为当前激活输入源。
2. 记下脚本列出的微信输入法真实 bundle id（不要猜）：

   ```bash
   ./venv/bin/python src/ominime/scripts/probe_ime.py --list-sources
   ```

3. 后台采样模式（推荐，无需手速同步）：先在终端启动采样，再切回任意 app
   （建议微信/Kim 输入框）用该输入法正常打字。脚本每 0.5 秒采样一次，只要
   任一次看到候选结构就永久记为峰值，**候选窗在你切回终端时消失也不影响结论**：

   ```bash
   ./venv/bin/python src/ominime/scripts/probe_ime.py --watch 30 --bundle-id <上一步得到的 bundle id>
   ```

   不带 `--bundle-id` 时脚本会默认使用当前激活输入源。采样结束后会打印峰值
   结构报告（`observed_via` 区分候选窗来自 AX还是 CGWindowList 兜底）。

   > 一次性快照模式（不加 `--watch`）依赖候选窗恰好显示、难以手动踩中，仅作备选。

## 结论（本机实测，2026-11-03）

探测环境：当前激活输入源为微信输入法 `com.tencent.inputmethod.wetype`（IME 进程
pid 558），`--watch 30` 后台采样期间正常打字产生候选窗。

- Gate result: `NOT PROVEN`
- 探测到的微信输入法 bundle id: `com.tencent.inputmethod.wetype`（本机另存旧版
  `com.tencent.inputmethod.chatterfly`，未激活）
- 候选文字是否可经 AX 采集（`candidate_text_collectible`）: **false**
- 观察途径（`observed_via`）: `cgwindowlist`（仅靠 Quartz 窗口列表看到候选窗几何）
- AX 窗口（`ax_window_count`）: **0** —— 直接读 `AXWindows` 返回 `(0, ())`，输入法
  进程不暴露任何 AX 应用窗口
- 候选窗是否单窗 / 几何可读 / 锚定输入区：均因正文不可采而无法评估
- `verdict`: `NOT_PROVEN:candidate_window_visible_but_no_ax_static_text`

## 决策

**微信输入法不接入注册表。** 现有候选读取链路（与豆包同一条）依赖从输入法进程的
AX 子树采集 `AXStaticText`；微信输入法不经 AX 暴露候选正文，即使强行加入
`IME_CANDIDATE_PROFILES` 也读不到候选词。因此：

- `IME_CANDIDATE_PROFILES` 保持仅豆包（`com.bytedance.inputmethod.doubaoime`），豆包行为不变；
- 微信输入法的输入仍由 Part A 的“发送后气泡确认”与通用提交捕获路径覆盖（不依赖候选词）。

> 复核提示：本结论来自单次 30s 采样。若后续微信输入法更新、可重新跑
> `--watch` 验证；只要 `observed_via` 仍为 `cgwindowlist` 且 `candidate_text_collectible`
> 为 false，即维持不接入。

## 探测脚本已知问题（已修）

- 早期 `list_keyboard_input_sources()` 的裸 ctypes 枚举未校验 CFString 类型，对某些
  输入源属性返回的非字符串指针直接按 CFString 处理，在 Homebrew Python 3.14 上会
  间歇性段错误。已加 `CFGetTypeID == CFStringGetTypeID` 类型门与长度/缓冲上界修复。

## verdict 取值含义

| verdict | 含义 |
|---|---|
| `PROVEN` | 单窗 + 可采集 + 几何可读 + 锚定成功，可接入 |
| `PROVEN_PENDING_ANCHOR` | 结构就绪，但未提供目标输入区几何，锚定待人工确认 |
| `NOT_PROVEN:ime_process_unavailable` | 未找到输入法进程 |
| `NOT_PROVEN:no_ax_windows` | 输入法进程无 AX 窗口 |
| `NOT_PROVEN:candidate_ax_unavailable` | 窗口内无可采集的 AXStaticText 候选 |
| `NOT_PROVEN:candidate_ax_bounds_unavailable` | 候选窗几何（position/size）不可读 |
| `NOT_PROVEN:ambiguous_ax_windows` | 多候选窗，无法唯一锚定 |
| `NOT_PROVEN:candidate_outside_composer` | 候选窗未落在目标输入区 |
| `NOT_PROVEN:no_candidate_window_observed` | 采样期间从未看到候选窗（未打字或未被 AX/CGWindowList 捕捉） |
| `NOT_PROVEN:candidate_window_visible_but_no_ax_static_text` | CGWindowList 看到候选窗几何，但 AX 不暴露候选正文（正文不可采，维持不接入） |
