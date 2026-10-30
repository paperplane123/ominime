"""
键盘监听模块

方案：
1. CGEventTap 监听键盘事件（英文、数字、符号、特殊键）
2. Rime 日志监听（中文输入法）
3. 系统唤醒事件监听，自动恢复 CGEventTap

需要用户授予辅助功能权限
"""

import threading
import time
import re
import uuid
from typing import Callable, Optional
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import queue

# macOS 原生 API
from Quartz import (
    CGEventTapCreate,
    CGEventTapEnable,
    CGEventTapIsEnabled,
    CFMachPortIsValid,
    CGEventGetIntegerValueField,
    CFMachPortCreateRunLoopSource,
    CFRunLoopAddSource,
    CFRunLoopRemoveSource,
    CFRunLoopGetCurrent,
    CFRunLoopRun,
    CFRunLoopStop,
    kCGSessionEventTap,
    kCGHeadInsertEventTap,
    kCGEventKeyDown,
    kCGEventKeyUp,
    kCGEventFlagsChanged,
    kCGEventLeftMouseDown,
    kCGEventRightMouseDown,
    kCGEventOtherMouseDown,
    kCGKeyboardEventKeycode,
    kCGEventFlagMaskShift,
    kCGEventFlagMaskControl,
    kCGEventFlagMaskAlternate,
    kCGEventFlagMaskCommand,
    CGEventGetFlags,
)
from AppKit import NSWorkspace, NSRunningApplication
from Foundation import (
    NSObject,
    NSRunLoop,
    NSDefaultRunLoopMode,
    NSDistributedNotificationCenter,
)
import Quartz
import objc

from .config import config
from .context_capture import (
    capture_accessibility_context,
    context_to_dict,
    focused_field_identity,
    is_secure_text_entry_context,
    is_text_entry_context,
    read_focused_text_via_range,
)
from .input_snapshot import format_submission_terminal_notice, normalize_submission_text
from .ime_candidate_capture import (
    DoubaoCandidateReader,
    DoubaoCompositionState,
    NUMBER_KEYCODE_TO_INDEX,
    SUPPORTED_TARGET_BUNDLE_IDS,
)
from .kim_composer_capture import LEGACY_KIM_BUNDLE_ID
from .wechat_composer_capture import WECHAT_BUNDLE_ID
from .chat_window_capture import ChatWindowBaselineSampler, session_anchors_match
from .post_send_capture import (
    CaptureOutcome,
    PostSendCaptureCoordinator,
    SendIntent,
    build_default_message_source_chain,
)
from .runtime_state import (
    record_runtime_error,
    refresh_runtime_heartbeat,
    set_recording_status,
)
from .time_utils import storage_now


EVENT_TAP_DISABLED_BY_TIMEOUT = getattr(Quartz, "kCGEventTapDisabledByTimeout", -1)
EVENT_TAP_DISABLED_BY_USER_INPUT = getattr(Quartz, "kCGEventTapDisabledByUserInput", -2)
KEYBOARD_EVENT_AUTOREPEAT_FIELD = getattr(Quartz, "kCGKeyboardEventAutorepeat", 8)


@dataclass
class KeyEvent:
    """按键事件"""

    timestamp: datetime
    keycode: int
    character: str
    app_name: str
    app_bundle_id: str
    modifiers: dict
    is_ime_input: bool = False


@dataclass(frozen=True)
class RawKeyboardEvent:
    """Small immutable sample safe to move off the EventTap callback thread."""

    event_type: int
    keycode: int
    text: str
    app_name: str
    bundle_id: str
    modifiers: dict
    target_pid: int = 0
    frontmost_pid: int = 0
    is_autorepeat: bool = False
    occurred_at: float = 0.0
    timestamp: datetime | None = None


@dataclass(frozen=True)
class PendingPostSendSubmission:
    intent_id: str
    app_name: str
    bundle_id: str
    target_pid: int
    key_modifiers: dict
    physical_key_count: int
    event_type: int
    baseline_window_id: int | None
    session_anchor: str | None


# 键码映射
SPECIAL_KEYCODE_MAP = {
    36: "\n",
    48: "\t",
    49: " ",
    51: "\b",
    53: "esc",
    117: "del",
    123: "←",
    124: "→",
    125: "↓",
    126: "↑",
    122: "F1",
    120: "F2",
    99: "F3",
    118: "F4",
    96: "F5",
    97: "F6",
    98: "F7",
    100: "F8",
    101: "F9",
    109: "F10",
    103: "F11",
    111: "F12",
}

IGNORED_KEYCODES = {54, 55, 56, 60, 58, 61, 59, 62, 57, 63}

KEYCODE_TO_CHAR = {
    0: "a",
    1: "s",
    2: "d",
    3: "f",
    4: "h",
    5: "g",
    6: "z",
    7: "x",
    8: "c",
    9: "v",
    11: "b",
    12: "q",
    13: "w",
    14: "e",
    15: "r",
    16: "y",
    17: "t",
    18: "1",
    19: "2",
    20: "3",
    21: "4",
    22: "6",
    23: "5",
    24: "=",
    25: "9",
    26: "7",
    27: "-",
    28: "8",
    29: "0",
    30: "]",
    31: "o",
    32: "u",
    33: "[",
    34: "i",
    35: "p",
    37: "l",
    38: "j",
    39: "'",
    40: "k",
    41: ";",
    42: "\\",
    43: ",",
    44: "/",
    45: "n",
    46: "m",
    47: ".",
    50: "`",
}


# 全局变量：当前活跃应用（通过应用切换通知更新）
_current_app_name = "Unknown"
_current_app_bundle = "unknown"
_current_app_pid = 0
_app_lock = threading.Lock()
_DEBUG = False  # 调试模式
_app_watcher_started = False
_app_activation_callback: Callable[[str, str, int], None] | None = None

# 最近接收键盘输入的应用（用于 Rime 中文输入归属）
_last_input_app_name = "Unknown"
_last_input_app_bundle = "unknown"
_last_input_lock = threading.Lock()

# 拼音检测：用于判断是否正在输入拼音
_pinyin_mode = False
_pinyin_mode_lock = threading.Lock()

# 拼音缓冲区：缓存可能是拼音的字母，如果没有 Rime 输出则作为英文处理
_pinyin_buffer = ""
_pinyin_buffer_app = ("Unknown", "unknown")
_pinyin_buffer_lock = threading.Lock()

# 只在 Enter 提交时读取完整输入框内容，避免记录拼音中间态。
ENTER_KEYCODE = 36
UNREADABLE_SUBMISSION_PLACEHOLDER = "[unreadable input]"
MAX_FALLBACK_BUFFER_CHARS = 4000
MAX_TEXT_FALLBACK_BUFFER_CHARS = 2000
MAX_KEY_EVENT_TEXT_CHARS = 64
MAX_RECENT_TEXT_SNAPSHOT_AGE_SECONDS = 60
MAX_POST_SEND_PASTE_VALIDATION_AGE_SECONDS = 5.0
TEXT_FALLBACK_EVENT_DEDUP_SECONDS = 0.2
MAX_TRUSTED_SUBMISSION_CHARS = 4000
DOUBAO_CANDIDATE_TIMEOUT_SECONDS = 5.0
EVENT_QUEUE_MAX_SIZE = 4096
APP_WATCHER_START_TIMEOUT_SECONDS = 5.0
EDITOR_NEWLINE_BUNDLE_PREFIXES = (
    "com.apple.TextEdit",
    "com.apple.Notes",
    "com.apple.iWork.Pages",
    "com.microsoft.Word",
    "com.microsoft.VSCode",
    "com.sublimetext",
    "com.todesktop.230313mzl4w4u92",  # Cursor
    "com.jetbrains.",
    "com.apple.dt.Xcode",
    "md.obsidian",
    "com.notion.Notion",
    "com.kingsoft.wpsoffice",
)
BROWSER_BUNDLE_IDS = {
    "com.apple.Safari",
    "com.google.Chrome",
    "org.mozilla.firefox",
    "com.brave.Browser",
    "com.microsoft.edgemac",
    "company.thebrowser.Browser",  # Arc
    "com.operasoftware.Opera",
    "com.vivaldi.Vivaldi",
}
SUBMISSION_TEXT_AREA_HINTS = (
    "chat",
    "command",
    "prompt",
    "query",
    "search",
)
BROWSER_BUNDLE_HINTS = (
    "arc",
    "brave",
    "browser",
    "chrome",
    "chromium",
    "edge",
    "firefox",
    "opera",
    "safari",
    "vivaldi",
)
POST_SEND_CHAT_BUNDLE_IDS = frozenset({LEGACY_KIM_BUNDLE_ID, WECHAT_BUNDLE_ID})
POST_SEND_BASELINE_WAIT_SECONDS = 0.5


def _clean_key_event_text(text: str) -> str:
    """Keep only printable text from a keyboard event."""
    return "".join(ch for ch in text if ch.isprintable())


def _contains_cjk(text: str) -> bool:
    """Return whether text contains committed CJK characters."""
    return any(
        ("\u3400" <= ch <= "\u4dbf")
        or ("\u4e00" <= ch <= "\u9fff")
        or ("\uf900" <= ch <= "\ufaff")
        for ch in text
    )


def _enter_is_editor_newline(app_name: str, bundle_id: str, context) -> bool:
    role = getattr(context, "focused_role", None)
    if isinstance(context, dict):
        role = context.get("focused_role")
    if role != "AXTextArea":
        return False
    normalized_bundle = bundle_id or ""
    if any(
        normalized_bundle == prefix or normalized_bundle.startswith(prefix)
        for prefix in EDITOR_NEWLINE_BUNDLE_PREFIXES
    ):
        return True
    normalized_bundle_casefold = normalized_bundle.casefold()
    is_browser = normalized_bundle in BROWSER_BUNDLE_IDS or any(
        hint in normalized_bundle_casefold for hint in BROWSER_BUNDLE_HINTS
    )
    if not is_browser:
        return False
    if isinstance(context, dict):
        semantic_text = " ".join(
            str(context.get(key) or "")
            for key in ("focused_identifier", "focused_title", "focused_description")
        )
    else:
        semantic_text = " ".join(
            str(getattr(context, key, None) or "")
            for key in ("focused_identifier", "focused_title", "focused_description")
        )
    normalized_semantics = semantic_text.casefold()
    return not any(hint in normalized_semantics for hint in SUBMISSION_TEXT_AREA_HINTS)


def set_last_input_app(name: str, bundle_id: str):
    """设置最近接收键盘输入的应用"""
    global _last_input_app_name, _last_input_app_bundle
    with _last_input_lock:
        _last_input_app_name = name
        _last_input_app_bundle = bundle_id


def get_last_input_app() -> tuple[str, str]:
    """获取最近接收键盘输入的应用"""
    with _last_input_lock:
        return (_last_input_app_name, _last_input_app_bundle)


def add_to_pinyin_buffer(char: str, app_name: str, bundle_id: str):
    """添加字符到拼音缓冲区"""
    global _pinyin_buffer, _pinyin_buffer_app
    with _pinyin_buffer_lock:
        _pinyin_buffer += char
        _pinyin_buffer_app = (app_name, bundle_id)


def clear_pinyin_buffer():
    """清空拼音缓冲区（Rime 已输出中文）"""
    global _pinyin_buffer
    with _pinyin_buffer_lock:
        _pinyin_buffer = ""


def flush_pinyin_buffer_as_english() -> tuple[str, str, str]:
    """将拼音缓冲区作为英文输出并清空，返回 (内容, app_name, bundle_id)"""
    global _pinyin_buffer, _pinyin_buffer_app
    with _pinyin_buffer_lock:
        content = _pinyin_buffer
        app = _pinyin_buffer_app
        _pinyin_buffer = ""
        return (content, app[0], app[1])


def set_pinyin_mode(is_pinyin: bool):
    """设置拼音输入模式"""
    global _pinyin_mode
    with _pinyin_mode_lock:
        _pinyin_mode = is_pinyin


def is_pinyin_mode() -> bool:
    """检查是否在拼音输入模式"""
    with _pinyin_mode_lock:
        return _pinyin_mode


def _set_app_activation_callback(
    callback: Callable[[str, str, int], None] | None,
):
    """Replace the listener notified by the process-wide app watcher."""
    global _app_activation_callback
    with _app_lock:
        _app_activation_callback = callback
        current = (_current_app_name, _current_app_bundle, _current_app_pid)
    if callback is not None and current[2] > 0:
        callback(*current)


def _refresh_app_activation_callback():
    with _app_lock:
        callback = _app_activation_callback
        current = (_current_app_name, _current_app_bundle, _current_app_pid)
    if callback is not None and current[2] > 0:
        callback(*current)


def _on_app_activated(name: str, bundle_id: str, pid: int = 0):
    """应用切换回调"""
    global _current_app_name, _current_app_bundle, _current_app_pid
    with _app_lock:
        if _DEBUG:
            print(f"[DEBUG] 应用切换: {_current_app_name} -> {name} ({bundle_id})")
        _current_app_name = name
        _current_app_bundle = bundle_id
        _current_app_pid = pid
        callback = _app_activation_callback
    if callback is not None and pid > 0:
        callback(name, bundle_id, pid)


def _start_app_watcher(
    activation_callback: Callable[[str, str, int], None] | None = None,
):
    """启动应用切换监听器（在单独线程中运行）"""
    global _app_watcher_started

    _set_app_activation_callback(activation_callback)
    if _app_watcher_started:
        return
    _app_watcher_started = True
    initialized = threading.Event()
    abort_watcher = threading.Event()
    initialization_errors: list[Exception] = []

    from Foundation import NSDate

    # 创建监听器类
    class AppWatcher(NSObject):
        def init(self):
            self = objc.super(AppWatcher, self).init()
            return self

        def applicationActivated_(self, notification):
            try:
                app = notification.userInfo()["NSWorkspaceApplicationKey"]
                name = app.localizedName() or "Unknown"
                bundle_id = app.bundleIdentifier() or "unknown"
                pid = int(app.processIdentifier())
                _on_app_activated(name, bundle_id, pid)
            except Exception as e:
                if _DEBUG:
                    print(f"[DEBUG] App watcher error: {e}")

    def run_watcher():
        try:
            watcher = AppWatcher.alloc().init()

            # 获取 workspace notification center
            ws = NSWorkspace.sharedWorkspace()
            nc = ws.notificationCenter()

            # 先注册切换通知，再初始化当前应用，避免启动窗口期漏事件。
            nc.addObserver_selector_name_object_(
                watcher,
                objc.selector(watcher.applicationActivated_, signature=b"v@:@"),
                "NSWorkspaceDidActivateApplicationNotification",
                None,
            )
            initialization_deadline = (
                time.monotonic() + APP_WATCHER_START_TIMEOUT_SECONDS
            )
            front_app = ws.frontmostApplication()
            while front_app is None and not abort_watcher.is_set():
                if time.monotonic() >= initialization_deadline:
                    raise RuntimeError("frontmost application unavailable")
                time.sleep(0.05)
                front_app = ws.frontmostApplication()
            if front_app is None:
                return
            _on_app_activated(
                front_app.localizedName() or "Unknown",
                front_app.bundleIdentifier() or "unknown",
                int(front_app.processIdentifier()),
            )
        except Exception as exc:
            initialization_errors.append(exc)
        finally:
            initialized.set()

        if initialization_errors or abort_watcher.is_set():
            return

        # 运行 RunLoop（应用切换由系统通知驱动）
        # 注意: runMode_beforeDate_ 可能在有 ready source 时立即返回，
        # 必须加 sleep 防止在 Rosetta 翻译下忙循环
        run_loop = NSRunLoop.currentRunLoop()
        while True:
            run_loop.runMode_beforeDate_(
                NSDefaultRunLoopMode, NSDate.dateWithTimeIntervalSinceNow_(1.0)
            )
            _refresh_app_activation_callback()
            time.sleep(0.1)  # 防止 RunLoop 立即返回导致忙循环

    thread = threading.Thread(target=run_watcher, daemon=True)
    thread.start()

    if not initialized.wait(timeout=APP_WATCHER_START_TIMEOUT_SECONDS):
        abort_watcher.set()
        _app_watcher_started = False
        _set_app_activation_callback(None)
        raise RuntimeError("app watcher initialization timed out")
    if initialization_errors:
        _app_watcher_started = False
        _set_app_activation_callback(None)
        raise RuntimeError(
            "app watcher initialization failed"
        ) from initialization_errors[0]


def get_frontmost_app() -> tuple[str, str]:
    """获取当前最前台的应用（直接调用 API）"""
    try:
        workspace = NSWorkspace.sharedWorkspace()
        app = workspace.frontmostApplication()
        if app:
            name = app.localizedName() or "Unknown"
            bundle_id = app.bundleIdentifier() or "unknown"
            return (name, bundle_id)
    except Exception as e:
        if _DEBUG:
            print(f"[DEBUG] get_frontmost_app error: {e}")
    return ("Unknown", "unknown")


def get_app_by_pid(pid: int) -> tuple[str, str]:
    """根据进程 ID 获取应用信息"""
    try:
        app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        if app:
            name = app.localizedName() or "Unknown"
            bundle_id = app.bundleIdentifier() or "unknown"
            return (name, bundle_id)
    except Exception as e:
        if _DEBUG:
            print(f"[DEBUG] get_app_by_pid error: {e}")
    return get_frontmost_app()  # 回退到 frontmost


def get_current_app() -> tuple[str, str]:
    """获取当前应用（从缓存读取，由应用切换通知更新）"""
    with _app_lock:
        return (_current_app_name, _current_app_bundle)


def get_current_app_target() -> tuple[str, str, int]:
    """Atomically read the cached frontmost application and its PID."""
    with _app_lock:
        return (_current_app_name, _current_app_bundle, _current_app_pid)


def get_current_app_fresh() -> tuple[str, str]:
    """获取当前应用（优先使用缓存，缓存无效时直接查询）"""
    with _app_lock:
        if _current_app_name != "Unknown":
            return (_current_app_name, _current_app_bundle)
    # 缓存无效，直接查询
    return get_frontmost_app()


class RimeLogWatcher:
    """监听 Rime 输入法日志文件"""

    RIME_LOG_PATH = Path.home() / ".ominime" / "rime_input.log"

    def __init__(self, callback: Callable[[str, datetime, str, str], None]):
        """callback: (text, timestamp, app_name, bundle_id) -> None"""
        self.callback = callback
        self._running = False
        self._thread = None
        self._last_position = 0
        self._last_mtime = 0

    def _ensure_log_file(self):
        self.RIME_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        if not self.RIME_LOG_PATH.exists():
            self.RIME_LOG_PATH.touch()

    def _parse_content(self, content: str) -> str:
        text = re.sub(r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]", "", content)
        return text

    def _watch_loop(self):
        self._ensure_log_file()

        try:
            self._last_position = self.RIME_LOG_PATH.stat().st_size
            self._last_mtime = self.RIME_LOG_PATH.stat().st_mtime
        except:
            self._last_position = 0
            self._last_mtime = 0

        while self._running:
            try:
                try:
                    current_mtime = self.RIME_LOG_PATH.stat().st_mtime
                except:
                    time.sleep(0.1)
                    continue

                if current_mtime > self._last_mtime:
                    self._last_mtime = current_mtime

                    # 使用最近接收键盘输入的应用（拼音输入时记录的目标应用）
                    app_name, bundle_id = get_last_input_app()
                    if app_name == "Unknown":
                        # 如果没有记录，回退到 frontmost
                        app_name, bundle_id = get_frontmost_app()

                    with open(
                        self.RIME_LOG_PATH, "r", encoding="utf-8", errors="ignore"
                    ) as f:
                        f.seek(self._last_position)
                        new_content = f.read()
                        self._last_position = f.tell()

                    if new_content:
                        text = self._parse_content(new_content)
                        if text and self.callback:
                            if _DEBUG:
                                print(
                                    f"[DEBUG] Rime 输入: '{text}' -> {app_name} ({bundle_id})"
                                )
                            self.callback(text, storage_now(), app_name, bundle_id)

                time.sleep(0.3)
            except Exception as e:
                if _DEBUG:
                    print(f"[DEBUG] Rime watch error: {e}")
                time.sleep(1.0)

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._watch_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=1.0)


class KeyboardListener:
    """全局键盘监听器

    包含以下健壮性机制：
    1. 定期健康检查 CGEventTap 状态
    2. 监听系统唤醒事件，自动恢复
    3. 自动重连机制
    """

    # 健康检查间隔（秒）
    HEALTH_CHECK_INTERVAL = 30
    # 最大重试次数
    MAX_RETRY_COUNT = 3

    def __init__(
        self,
        callback: Callable[[KeyEvent], None],
        diagnostics_callback: Optional[Callable[[dict], None]] = None,
        candidate_reader: DoubaoCandidateReader | None = None,
        post_send_coordinator: PostSendCaptureCoordinator | None = None,
        baseline_sampler: ChatWindowBaselineSampler | None = None,
    ):
        self.callback = callback
        self.diagnostics_callback = diagnostics_callback
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._health_check_thread: Optional[threading.Thread] = None
        self._run_loop = None
        self._run_loop_source = None
        self._tap = None
        self._rime_watcher = RimeLogWatcher(self._on_rime_input)
        self._last_event_time = time.time()
        self._tap_lock = threading.Lock()
        self._retry_count = 0
        self._wake_observer = None
        self._last_empty_submission_log = 0.0
        self._fallback_buffers: dict[tuple[str, str], list[str]] = {}
        self._fallback_buffer_updated_at: dict[tuple[str, str], float] = {}
        self._text_fallback_buffers: dict[tuple[str, str], list[str]] = {}
        self._text_fallback_buffer_updated_at: dict[tuple[str, str], float] = {}
        self._recent_text_snapshots: dict[
            tuple[str, str], tuple[str, float, str | None]
        ] = {}
        self._recent_paste_validations: dict[
            tuple[str, str], tuple[int, float]
        ] = {}
        self._pending_post_send_paste_keys: set[tuple[str, str]] = set()
        self._post_send_paste_snapshot_keys: set[tuple[str, str]] = set()
        self._tainted_post_send_validation_keys: set[tuple[str, str]] = set()
        self._active_field_ids: dict[tuple[str, str], str | None] = {}
        self._text_fallback_field_ids: dict[tuple[str, str], str | None] = {}
        self._fallback_field_ids: dict[tuple[str, str], str | None] = {}
        self._last_text_fallback_events: dict[
            tuple[str, str], tuple[int, str, float]
        ] = {}
        self._pending_enter_keyups: set[tuple[str, str]] = set()
        self._candidate_reader = candidate_reader or DoubaoCandidateReader()
        self._post_send_coordinator = post_send_coordinator
        self._baseline_sampler = baseline_sampler
        self._owns_post_send_coordinator = post_send_coordinator is None
        self._owns_baseline_sampler = baseline_sampler is None
        self._post_send_pending: dict[str, PendingPostSendSubmission] = {}
        self._post_send_pending_lock = threading.Lock()
        self._target_app_identities: dict[int, tuple[str, str]] = {}
        self._last_warmed_post_send_target: tuple[str, str, int] | None = None
        self._doubao_states: dict[tuple[str, str], DoubaoCompositionState] = {}
        self._doubao_failure_reasons: dict[tuple[str, str], str] = {}
        self._doubao_arrow_read_attempts: set[tuple[str, str]] = set()
        self._doubao_untrusted_candidate_selections: set[tuple[str, str]] = set()
        self._last_doubao_target: tuple[str, str, int] | None = None
        self._event_queue: queue.Queue[RawKeyboardEvent | None] = queue.Queue(
            maxsize=EVENT_QUEUE_MAX_SIZE
        )
        self._event_worker_thread: Optional[threading.Thread] = None
        self._event_worker_running = False
        self._has_started = False
        self._event_processing_lock = threading.Lock()
        self._dropped_event_count = 0

    def _ensure_post_send_services(self) -> None:
        if self._baseline_sampler is None and self._owns_baseline_sampler:
            self._baseline_sampler = ChatWindowBaselineSampler()
        if (
            self._post_send_coordinator is None
            and self._owns_post_send_coordinator
            and self._baseline_sampler is not None
        ):
            source_chain = build_default_message_source_chain(
                frame_provider=self._baseline_sampler.capture_current_frame,
            )
            self._post_send_coordinator = PostSendCaptureCoordinator(
                source_chain,
                on_success=self._handle_post_send_success,
                on_diagnostic=self._handle_post_send_failure,
            )

    def _prepare_activated_composer(
        self,
        app_name: str,
        bundle_id: str,
        target_pid: int,
    ) -> None:
        """Resolve composer window metadata outside the EventTap callback."""
        if target_pid <= 0:
            return
        warmed_target = (
            (app_name, bundle_id, target_pid)
            if bundle_id in POST_SEND_CHAT_BUNDLE_IDS
            else None
        )
        previous_warmed_target = self._last_warmed_post_send_target
        self._last_warmed_post_send_target = warmed_target
        if bundle_id in POST_SEND_CHAT_BUNDLE_IDS:
            self._target_app_identities[target_pid] = (app_name, bundle_id)
            if warmed_target != previous_warmed_target:
                self._schedule_post_send_baseline(app_name, bundle_id, target_pid)

    def _on_rime_input(
        self, text: str, timestamp: datetime, app_name: str, bundle_id: str
    ):
        """Rime log events are ignored in submission-snapshot mode."""
        return

    def _copy_ax_attribute(self, element, attribute: str):
        """Read an Accessibility attribute across PyObjC signature variants."""
        try:
            from ApplicationServices import AXUIElementCopyAttributeValue

            try:
                result = AXUIElementCopyAttributeValue(element, attribute, None)
            except TypeError:
                result = AXUIElementCopyAttributeValue(element, attribute)

            if isinstance(result, tuple):
                if len(result) >= 2 and result[0] == 0:
                    return result[1]
                return None
            return result
        except Exception as e:
            if _DEBUG:
                print(f"[DEBUG] AX read attribute failed: {attribute}: {e}")
            return None

    def _get_focused_text_snapshot(self) -> str:
        """Read current focused input value for IME/voice commit diffing."""
        try:
            from ApplicationServices import AXUIElementCreateSystemWide

            system = AXUIElementCreateSystemWide()
            focused = self._copy_ax_attribute(system, "AXFocusedUIElement")
            if focused is None:
                return ""

            value = self._copy_ax_attribute(focused, "AXValue")
            if isinstance(value, str):
                return value
        except Exception as e:
            if _DEBUG:
                print(f"[DEBUG] focused text snapshot failed: {e}")
        return ""

    def _capture_focused_context(
        self,
        *,
        max_depth: int | None = None,
        target_pid: int | None = None,
    ):
        kwargs = {}
        if max_depth is not None:
            kwargs["max_depth"] = max_depth
        if target_pid is not None and target_pid > 0:
            kwargs["target_pid"] = target_pid
        if not kwargs:
            return capture_accessibility_context()
        try:
            return capture_accessibility_context(**kwargs)
        except TypeError:
            # Lightweight test doubles and older capture implementations may
            # only expose the zero-argument form.
            return capture_accessibility_context()

    def _context_to_dict_safe(self, context) -> dict:
        try:
            return context_to_dict(context)
        except Exception:
            return {}

    def _get_event_target_app(self, event) -> tuple[str, str]:
        target_pid = self._get_event_target_pid(event)
        if target_pid > 0:
            return get_app_by_pid(target_pid)
        return get_frontmost_app()

    def _get_event_target_pid(self, event) -> int:
        return int(
            CGEventGetIntegerValueField(event, 40) or 0
        )  # kCGEventTargetUnixProcessID

    def _fallback_buffer_key(self, app_name: str, bundle_id: str) -> tuple[str, str]:
        return (app_name or "Unknown", bundle_id or "unknown")

    def _doubao_state(
        self,
        app_name: str,
        bundle_id: str,
    ) -> DoubaoCompositionState | None:
        if bundle_id not in SUPPORTED_TARGET_BUNDLE_IDS:
            return None
        key = self._fallback_buffer_key(app_name, bundle_id)
        state = self._doubao_states.get(key)
        if state is None:
            state = DoubaoCompositionState(
                timeout_seconds=config.session_timeout,
                candidate_timeout_seconds=DOUBAO_CANDIDATE_TIMEOUT_SECONDS,
            )
            self._doubao_states[key] = state
        return state

    def _update_doubao_target(
        self,
        app_name: str,
        bundle_id: str,
        target_pid: int,
    ):
        target = (
            (app_name, bundle_id, target_pid)
            if bundle_id in SUPPORTED_TARGET_BUNDLE_IDS and target_pid > 0
            else None
        )
        if self._last_doubao_target is not None and target != self._last_doubao_target:
            for state in self._doubao_states.values():
                state.clear()
            self._doubao_states.clear()
            self._doubao_failure_reasons.clear()
            self._doubao_arrow_read_attempts.clear()
            self._doubao_untrusted_candidate_selections.clear()
        self._last_doubao_target = target

    def _clear_doubao_state(self, app_name: str, bundle_id: str):
        key = self._fallback_buffer_key(app_name, bundle_id)
        state = self._doubao_states.pop(key, None)
        self._doubao_failure_reasons.pop(key, None)
        self._doubao_arrow_read_attempts.discard(key)
        self._doubao_untrusted_candidate_selections.discard(key)
        if state is not None:
            state.clear()

    def _pop_doubao_submission(
        self,
        app_name: str,
        bundle_id: str,
        target_pid: int | None,
    ) -> str:
        key = self._fallback_buffer_key(app_name, bundle_id)
        state = self._doubao_states.pop(key, None)
        selection_is_untrusted = key in self._doubao_untrusted_candidate_selections
        self._doubao_arrow_read_attempts.discard(key)
        self._doubao_untrusted_candidate_selections.discard(key)
        if state is None or target_pid is None or target_pid <= 0:
            return ""
        if selection_is_untrusted:
            state.clear()
            return ""
        content = state.pop_submission(target_pid=target_pid)
        state.clear()
        return content

    def _handle_doubao_keydown(
        self,
        app_name: str,
        bundle_id: str,
        target_pid: int,
        keycode: int,
        event_text: str,
        modifiers: dict,
        is_autorepeat: bool = False,
    ):
        state = self._doubao_state(app_name, bundle_id)
        if state is None or target_pid <= 0:
            return None
        if modifiers.get("cmd") or modifiers.get("ctrl") or modifiers.get("alt"):
            return None
        key = self._fallback_buffer_key(app_name, bundle_id)
        candidate_commit_keycodes = {
            ENTER_KEYCODE,
            49,
            *NUMBER_KEYCODE_TO_INDEX,
        }
        is_candidate_arrow = keycode in {123, 124}
        should_read_candidate_arrow = (
            is_candidate_arrow
            and not is_autorepeat
            and not state.has_active_candidate
            and key not in self._doubao_arrow_read_attempts
        )
        if should_read_candidate_arrow:
            self._doubao_arrow_read_attempts.add(key)
        if keycode in candidate_commit_keycodes or should_read_candidate_arrow:
            snapshot = self._candidate_reader.read(
                target_pid=target_pid,
                target_bundle_id=bundle_id,
            )
            if snapshot is None:
                self._doubao_failure_reasons[key] = (
                    getattr(
                        self._candidate_reader,
                        "last_failure_reason",
                        None,
                    )
                    or "candidate_unavailable"
                )
                if is_candidate_arrow:
                    self._doubao_untrusted_candidate_selections.add(key)
                if state.has_active_candidate:
                    state.clear()
                    return None
            else:
                if key not in self._doubao_untrusted_candidate_selections:
                    self._doubao_failure_reasons.pop(key, None)
                state.update_candidates(snapshot, target_pid=target_pid)
                if is_candidate_arrow and not state.has_active_candidate:
                    self._doubao_untrusted_candidate_selections.add(key)
        elif is_candidate_arrow and not state.has_active_candidate:
            self._doubao_untrusted_candidate_selections.add(key)
        result = state.handle_key(
            keycode=keycode,
            text=event_text,
            target_pid=target_pid,
        )
        handled_keycodes = {
            ENTER_KEYCODE,
            49,  # Space
            51,  # Backspace
            123,
            124,
            125,
            126,
            *NUMBER_KEYCODE_TO_INDEX,
        }
        if keycode not in handled_keycodes and keycode in KEYCODE_TO_CHAR:
            if not state.pending_preedit and not state.has_active_candidate:
                self._doubao_arrow_read_attempts.discard(key)
            state.record_printable(
                event_text or KEYCODE_TO_CHAR[keycode],
                target_pid=target_pid,
            )
        return result

    def _refresh_doubao_candidates(
        self,
        app_name: str,
        bundle_id: str,
        target_pid: int,
        keycode: int,
        modifiers: dict,
    ):
        state = self._doubao_state(app_name, bundle_id)
        if state is None or target_pid <= 0:
            return
        if modifiers.get("cmd") or modifiers.get("ctrl") or modifiers.get("alt"):
            return
        if keycode in {49, 123, 124, 125, 126, *NUMBER_KEYCODE_TO_INDEX}:
            return
        snapshot = self._candidate_reader.read(
            target_pid=target_pid,
            target_bundle_id=bundle_id,
        )
        key = self._fallback_buffer_key(app_name, bundle_id)
        if snapshot is None:
            self._doubao_failure_reasons[key] = (
                getattr(
                    self._candidate_reader,
                    "last_failure_reason",
                    None,
                )
                or "candidate_unavailable"
            )
        else:
            self._doubao_failure_reasons.pop(key, None)
        state.update_candidates(snapshot, target_pid=target_pid)

    def _is_fallback_buffer_expired(self, updated_at: float | None) -> bool:
        if updated_at is None:
            return False
        return time.monotonic() - updated_at > config.session_timeout

    def _get_keyboard_event_text(self, event) -> str:
        getter = getattr(Quartz, "CGEventKeyboardGetUnicodeString", None)
        if getter is None:
            return ""
        try:
            actual_length, text = getter(event, MAX_KEY_EVENT_TEXT_CHARS, None, None)
        except Exception:
            self._dropped_event_count += 1
            return ""

        if not text:
            return ""
        return _clean_key_event_text(text[:actual_length])

    def _record_text_fallback_key(
        self,
        app_name: str,
        bundle_id: str,
        keycode: int,
        modifiers: dict,
        event_text: str,
        *,
        track_editing_keys: bool = True,
    ):
        """Track real Unicode text from key events when AXValue is unavailable."""
        if not getattr(config, "capture_key_event_text_fallback", True):
            return
        if config.is_app_ignored(bundle_id):
            return
        if modifiers.get("cmd") or modifiers.get("ctrl") or modifiers.get("alt"):
            return

        key = self._fallback_buffer_key(app_name, bundle_id)
        if self._is_fallback_buffer_expired(
            self._text_fallback_buffer_updated_at.get(key)
        ):
            self._text_fallback_buffers.pop(key, None)

        if track_editing_keys and keycode == 51:  # Backspace
            buffer = self._text_fallback_buffers.get(key)
            if buffer:
                buffer.pop()
                self._text_fallback_buffer_updated_at[key] = time.monotonic()
            return

        text = event_text
        if not text or not _contains_cjk(text):
            return

        now = time.monotonic()
        previous = self._last_text_fallback_events.get(key)
        if (
            previous is not None
            and previous[0] == keycode
            and previous[1] == text
            and now - previous[2] <= TEXT_FALLBACK_EVENT_DEDUP_SECONDS
        ):
            self._last_text_fallback_events[key] = (keycode, text, now)
            return

        buffer = self._text_fallback_buffers.setdefault(key, [])
        self._text_fallback_field_ids[key] = self._active_field_ids.get(key)
        buffer.append(text)
        joined_len = sum(len(part) for part in buffer)
        while buffer and joined_len > MAX_TEXT_FALLBACK_BUFFER_CHARS:
            joined_len -= len(buffer.pop(0))
        self._text_fallback_buffer_updated_at[key] = now
        self._last_text_fallback_events[key] = (keycode, text, now)

    def _pop_text_fallback_content(
        self,
        app_name: str,
        bundle_id: str,
        current_field_id: str | None = None,
        *,
        allow_unscoped: bool = False,
    ) -> str:
        key = self._fallback_buffer_key(app_name, bundle_id)
        updated_at = self._text_fallback_buffer_updated_at.pop(key, None)
        buffer = self._text_fallback_buffers.pop(key, [])
        buffer_field_id = self._text_fallback_field_ids.pop(key, None)
        self._last_text_fallback_events.pop(key, None)
        if current_field_id is None and buffer_field_id is None:
            if not allow_unscoped:
                return ""
        elif (
            current_field_id is None
            or buffer_field_id is None
            or current_field_id != buffer_field_id
        ):
            return ""
        if self._is_fallback_buffer_expired(updated_at):
            return ""
        content = "".join(buffer)
        if not _contains_cjk(content):
            return ""
        return content

    def _record_recent_text_snapshot(
        self, app_name: str, bundle_id: str, *, clear_on_empty: bool = False,
        target_pid: int = 0,
    ):
        key = self._fallback_buffer_key(app_name, bundle_id)
        context = self._capture_focused_context(
            max_depth=1, target_pid=target_pid if target_pid > 0 else None
        )
        if not is_text_entry_context(context):
            self._recent_text_snapshots.pop(key, None)
            return
        if is_secure_text_entry_context(context):
            self._clear_submission_buffers(app_name, bundle_id)
            return

        field_id = focused_field_identity(context)
        previous_field_id = self._active_field_ids.get(key)
        if (
            field_id is not None
            and previous_field_id is not None
            and field_id != previous_field_id
        ):
            self._clear_submission_buffers(app_name, bundle_id)
        self._active_field_ids[key] = field_id

        focused_value = getattr(context, "focused_value", None)
        if not isinstance(focused_value, str):
            focused_value = self._get_focused_text_snapshot()
        if (
            not focused_value
            and bundle_id == "com.apple.Safari"
            and target_pid > 0
        ):
            focused_value = read_focused_text_via_range(
                target_pid, max_chars=MAX_TRUSTED_SUBMISSION_CHARS
            )
        content = normalize_submission_text(
            focused_value,
            app_name=app_name,
            bundle_id=bundle_id,
        )
        if content and len(content) <= MAX_TRUSTED_SUBMISSION_CHARS:
            self._recent_text_snapshots[key] = (content, time.monotonic(), field_id)
        elif clear_on_empty:
            self._recent_text_snapshots.pop(key, None)

    @staticmethod
    def _read_post_send_paste_text() -> str:
        try:
            from AppKit import NSPasteboard, NSPasteboardTypeString

            value = NSPasteboard.generalPasteboard().stringForType_(
                NSPasteboardTypeString
            )
        except Exception:
            return ""
        return value if isinstance(value, str) else ""

    @staticmethod
    def _get_post_send_paste_change_count() -> int:
        try:
            from AppKit import NSPasteboard

            return int(NSPasteboard.generalPasteboard().changeCount())
        except Exception:
            return -1

    def _tracked_post_send_composer_is_empty(
        self,
        app_name: str,
        bundle_id: str,
    ) -> bool:
        key = self._fallback_buffer_key(app_name, bundle_id)
        return not (
            self._recent_text_snapshots.get(key)
            or self._recent_paste_validations.get(key)
            or self._fallback_buffers.get(key)
            or self._text_fallback_buffers.get(key)
            or self._doubao_states.get(key)
        )

    def _record_post_send_paste_validation(
        self,
        app_name: str,
        bundle_id: str,
    ) -> None:
        key = self._fallback_buffer_key(app_name, bundle_id)
        if key not in self._pending_post_send_paste_keys:
            self._recent_paste_validations.pop(key, None)
            return
        self._pending_post_send_paste_keys.discard(key)
        change_count = self._get_post_send_paste_change_count()
        if change_count < 0:
            self._recent_paste_validations.pop(key, None)
            return
        self._recent_paste_validations[key] = (change_count, time.monotonic())

    def _pop_post_send_paste_validation(
        self,
        app_name: str,
        bundle_id: str,
    ) -> str:
        snapshot = self._recent_paste_validations.pop(
            self._fallback_buffer_key(app_name, bundle_id),
            None,
        )
        if snapshot is None:
            return ""
        change_count, updated_at = snapshot
        if time.monotonic() - updated_at > MAX_POST_SEND_PASTE_VALIDATION_AGE_SECONDS:
            return ""
        if self._get_post_send_paste_change_count() != change_count:
            return ""
        content = normalize_submission_text(
            self._read_post_send_paste_text(),
            app_name=app_name,
            bundle_id=bundle_id,
        )
        if len(content) > MAX_TRUSTED_SUBMISSION_CHARS:
            return ""
        return content

    def _pop_recent_text_snapshot_content(
        self,
        app_name: str,
        bundle_id: str,
        current_field_id: str | None = None,
        *,
        allow_unscoped: bool = False,
    ) -> str:
        key = self._fallback_buffer_key(app_name, bundle_id)
        snapshot = self._recent_text_snapshots.pop(key, None)
        if snapshot is None:
            return ""
        if (
            len(snapshot) == 2
        ):  # Compatibility with snapshots created before field scoping.
            content, updated_at = snapshot
            snapshot_field_id = None
        else:
            content, updated_at, snapshot_field_id = snapshot
        if current_field_id is None and snapshot_field_id is None:
            if not allow_unscoped:
                return ""
        elif (
            current_field_id is None
            or snapshot_field_id is None
            or current_field_id != snapshot_field_id
        ):
            return ""
        if time.monotonic() - updated_at > MAX_RECENT_TEXT_SNAPSHOT_AGE_SECONDS:
            return ""
        if len(content) > MAX_TRUSTED_SUBMISSION_CHARS:
            return ""
        return content

    def _clear_recent_text_snapshot(self, app_name: str, bundle_id: str):
        key = self._fallback_buffer_key(app_name, bundle_id)
        self._recent_text_snapshots.pop(key, None)

    def _clear_text_fallback_buffer(self, app_name: str, bundle_id: str):
        key = self._fallback_buffer_key(app_name, bundle_id)
        self._text_fallback_buffers.pop(key, None)
        self._text_fallback_buffer_updated_at.pop(key, None)
        self._text_fallback_field_ids.pop(key, None)
        self._last_text_fallback_events.pop(key, None)

    def _record_fallback_key(
        self,
        app_name: str,
        bundle_id: str,
        keycode: int,
        modifiers: dict,
        event_text: str = "",
    ):
        """Track typed key count for apps whose Accessibility value is unreadable."""
        if config.is_app_ignored(bundle_id):
            return
        if modifiers.get("cmd") or modifiers.get("ctrl") or modifiers.get("alt"):
            return

        key = self._fallback_buffer_key(app_name, bundle_id)
        if self._is_fallback_buffer_expired(self._fallback_buffer_updated_at.get(key)):
            self._fallback_buffers.pop(key, None)
        buffer = self._fallback_buffers.setdefault(key, [])
        if keycode == 51:  # Backspace
            if buffer:
                buffer.pop()
                self._fallback_buffer_updated_at[key] = time.monotonic()
            return
        if keycode == 49:  # Space
            char = " "
        elif event_text and all(character.isprintable() for character in event_text):
            char = event_text
        elif keycode in KEYCODE_TO_CHAR:
            char = KEYCODE_TO_CHAR[keycode]
        else:
            return

        buffer.append(char)
        self._fallback_field_ids[key] = self._active_field_ids.get(key)
        if len(buffer) > MAX_FALLBACK_BUFFER_CHARS:
            del buffer[: len(buffer) - MAX_FALLBACK_BUFFER_CHARS]
        self._fallback_buffer_updated_at[key] = time.monotonic()

    def _record_post_send_newline(self, app_name: str, bundle_id: str) -> None:
        key = self._fallback_buffer_key(app_name, bundle_id)
        buffer = self._fallback_buffers.setdefault(key, [])
        buffer.append("\n")
        self._fallback_field_ids[key] = self._active_field_ids.get(key)
        self._fallback_buffer_updated_at[key] = time.monotonic()

    def _pop_fallback_count(
        self,
        app_name: str,
        bundle_id: str,
        current_field_id: str | None = None,
    ) -> int:
        key = self._fallback_buffer_key(app_name, bundle_id)
        updated_at = self._fallback_buffer_updated_at.pop(key, None)
        buffer = self._fallback_buffers.pop(key, [])
        buffer_field_id = self._fallback_field_ids.pop(key, None)
        if (current_field_id is None) != (buffer_field_id is None) or (
            current_field_id is not None
            and buffer_field_id is not None
            and current_field_id != buffer_field_id
        ):
            return 0
        if self._is_fallback_buffer_expired(updated_at):
            return 0
        return len(buffer)

    def _peek_fallback_content(
        self,
        app_name: str,
        bundle_id: str,
        current_field_id: str | None = None,
    ) -> str:
        key = self._fallback_buffer_key(app_name, bundle_id)
        updated_at = self._fallback_buffer_updated_at.get(key)
        buffer_field_id = self._fallback_field_ids.get(key)
        if (
            (current_field_id is None) != (buffer_field_id is None)
            or (
                current_field_id is not None
                and buffer_field_id is not None
                and current_field_id != buffer_field_id
            )
            or self._is_fallback_buffer_expired(updated_at)
        ):
            return ""
        return "".join(self._fallback_buffers.get(key, ()))

    def _clear_fallback_buffer(self, app_name: str, bundle_id: str):
        key = self._fallback_buffer_key(app_name, bundle_id)
        self._fallback_buffers.pop(key, None)
        self._fallback_buffer_updated_at.pop(key, None)
        self._fallback_field_ids.pop(key, None)

    def _clear_submission_buffers(self, app_name: str, bundle_id: str):
        key = self._fallback_buffer_key(app_name, bundle_id)
        self._recent_paste_validations.pop(key, None)
        self._pending_post_send_paste_keys.discard(key)
        self._post_send_paste_snapshot_keys.discard(key)
        self._tainted_post_send_validation_keys.discard(key)
        self._clear_recent_text_snapshot(app_name, bundle_id)
        self._clear_text_fallback_buffer(app_name, bundle_id)
        self._clear_fallback_buffer(app_name, bundle_id)
        self._clear_doubao_state(app_name, bundle_id)

    def _ignore_enter_keyup_once(self, app_name: str, bundle_id: str):
        key = self._fallback_buffer_key(app_name, bundle_id)
        self._pending_enter_keyups.add(key)

    def _event_type_name(self, event_type) -> str:
        if event_type == kCGEventKeyDown:
            return "enter_keydown"
        if event_type == kCGEventKeyUp:
            return "enter_keyup"
        return "enter_unknown"

    def _emit_capture_diagnostic(
        self,
        app_name: str,
        bundle_id: str,
        *,
        event_type,
        decision_action: str,
        decision_reason: str,
        selected_source: str | None = None,
        selected_confidence: float | None = None,
        physical_key_count: int | None = None,
        capture_status: str = "ok",
        diagnostics: dict | None = None,
        context_data: dict | None = None,
    ):
        if self.diagnostics_callback is None:
            return
        context_data = context_data or {}
        diagnostic_details = dict(diagnostics or {})
        capture_error = context_data.get("capture_error")
        if capture_error:
            diagnostic_details["capture_error"] = capture_error
        self.diagnostics_callback(
            {
                "timestamp": storage_now(),
                "app_name": app_name,
                "app_bundle_id": bundle_id,
                "event_type": self._event_type_name(event_type),
                "decision_action": decision_action,
                "decision_reason": decision_reason,
                "selected_source": selected_source,
                "selected_confidence": selected_confidence,
                "physical_key_count": physical_key_count,
                "focused_role": context_data.get("focused_role"),
                "focused_subrole": context_data.get("focused_subrole"),
                "capture_status": context_data.get("capture_status") or capture_status,
                "diagnostics": diagnostic_details,
            }
        )

    def _should_ignore_enter_keyup(
        self, app_name: str, bundle_id: str, event_type
    ) -> bool:
        if event_type != kCGEventKeyUp:
            return False
        key = self._fallback_buffer_key(app_name, bundle_id)
        if key not in self._pending_enter_keyups:
            return False
        self._pending_enter_keyups.remove(key)
        return True

    def _event_modifiers(self, event) -> dict:
        flags = CGEventGetFlags(event) or 0
        return {
            "shift": bool(flags & kCGEventFlagMaskShift),
            "ctrl": bool(flags & kCGEventFlagMaskControl),
            "alt": bool(flags & kCGEventFlagMaskAlternate),
            "cmd": bool(flags & kCGEventFlagMaskCommand),
        }

    def _emit_submission_event(
        self,
        *,
        app_name: str,
        bundle_id: str,
        content: str,
        key_modifiers: dict,
        context_data: dict,
        fallback_source: str | None = None,
        char_count_override: int | None = None,
        redacted_content: bool = False,
        physical_key_count: int | None = None,
        capture_diagnostics: dict | None = None,
    ):
        modifiers = {
            "shift": key_modifiers.get("shift", False),
            "ctrl": key_modifiers.get("ctrl", False),
            "alt": key_modifiers.get("alt", False),
            "cmd": key_modifiers.get("cmd", False),
            "submit_snapshot": True,
            "submission_id": uuid.uuid4().hex,
            "context": context_data,
            "redacted_content": redacted_content,
        }
        if fallback_source is not None:
            modifiers["fallback_source"] = fallback_source
        if char_count_override is not None:
            modifiers["char_count_override"] = char_count_override
        if physical_key_count is not None:
            modifiers["physical_key_count"] = physical_key_count
        if capture_diagnostics:
            modifiers["capture_diagnostics"] = dict(capture_diagnostics)
        key_event = KeyEvent(
            timestamp=storage_now(),
            keycode=ENTER_KEYCODE,
            character=content,
            app_name=app_name,
            app_bundle_id=bundle_id,
            modifiers=modifiers,
            is_ime_input=True,
        )
        if self.callback:
            self.callback(key_event)

    def _schedule_post_send_baseline(
        self,
        app_name: str,
        bundle_id: str,
        target_pid: int,
    ) -> None:
        if (
            self._baseline_sampler is None
            or target_pid <= 0
            or config.input_capture_mode != "enter-text"
        ):
            return
        context = self._capture_focused_context(
            max_depth=0,
            target_pid=target_pid,
        )
        if is_secure_text_entry_context(context):
            return
        self._baseline_sampler.schedule(target_pid)

    def _process_post_send_chat_enter(
        self,
        raw_event: RawKeyboardEvent,
        app_name: str,
        bundle_id: str,
        target_pid: int,
    ) -> None:
        if self._post_send_coordinator is None:
            return
        if config.input_capture_mode != "enter-text":
            self._clear_submission_buffers(app_name, bundle_id)
            return

        context = self._capture_focused_context(target_pid=target_pid)
        current_field_id = focused_field_identity(context)
        if is_secure_text_entry_context(context):
            physical_key_count = self._pop_fallback_count(
                app_name,
                bundle_id,
                current_field_id=current_field_id,
            )
            self._clear_submission_buffers(app_name, bundle_id)
            self._emit_capture_diagnostic(
                app_name,
                bundle_id,
                event_type=raw_event.event_type,
                decision_action="skip",
                decision_reason="secure_text_input",
                physical_key_count=physical_key_count,
            )
            return

        validation_key = self._fallback_buffer_key(app_name, bundle_id)
        validation_tainted = validation_key in self._tainted_post_send_validation_keys
        if validation_tainted:
            self._clear_recent_text_snapshot(app_name, bundle_id)
            self._recent_paste_validations.pop(validation_key, None)
        fallback_validation_text = (
            ""
            if validation_tainted
            else self._peek_fallback_content(
                app_name,
                bundle_id,
                current_field_id=current_field_id,
            )
        )
        physical_key_count = self._pop_fallback_count(
            app_name,
            bundle_id,
            current_field_id=current_field_id,
        )
        validation_text = (
            ""
            if validation_tainted
            else normalize_submission_text(
                self._pop_recent_text_snapshot_content(
                    app_name,
                    bundle_id,
                    current_field_id=current_field_id,
                    allow_unscoped=True,
                )
                or self._pop_post_send_paste_validation(app_name, bundle_id)
                or self._pop_doubao_submission(app_name, bundle_id, target_pid)
                or self._pop_text_fallback_content(
                    app_name,
                    bundle_id,
                    current_field_id=current_field_id,
                    allow_unscoped=True,
                )
                or fallback_validation_text,
                app_name=app_name,
                bundle_id=bundle_id,
            )
        )
        baseline = (
            self._baseline_sampler.take_baseline(
                target_pid,
                wait_timeout=POST_SEND_BASELINE_WAIT_SECONDS,
            )
            if self._baseline_sampler is not None
            else None
        )
        self._clear_submission_buffers(app_name, bundle_id)
        intent_id = uuid.uuid4().hex
        intent = SendIntent(
            intent_id=intent_id,
            submitted_at=(
                raw_event.occurred_at if raw_event.occurred_at > 0 else time.monotonic()
            ),
            timestamp=raw_event.timestamp or storage_now(),
            app_name=app_name,
            bundle_id=bundle_id,
            target_pid=target_pid,
            modifiers=raw_event.modifiers,
            physical_key_count=physical_key_count,
            validation_text=validation_text,
            baseline=baseline,
        )
        pending = PendingPostSendSubmission(
            intent_id=intent_id,
            app_name=app_name,
            bundle_id=bundle_id,
            target_pid=target_pid,
            key_modifiers=dict(raw_event.modifiers),
            physical_key_count=physical_key_count,
            event_type=raw_event.event_type,
            baseline_window_id=getattr(baseline, "window_id", None),
            session_anchor=getattr(baseline, "session_anchor", None),
        )
        with self._post_send_pending_lock:
            self._post_send_pending[intent_id] = pending
        if self._post_send_coordinator.submit(intent):
            return
        with self._post_send_pending_lock:
            self._post_send_pending.pop(intent_id, None)
        self._emit_capture_diagnostic(
            app_name,
            bundle_id,
            event_type=raw_event.event_type,
            decision_action="skip",
            decision_reason="post_send_queue_full",
            physical_key_count=physical_key_count,
            diagnostics={"intent_id": intent_id},
        )

    def _pop_post_send_pending(
        self,
        intent_id: str,
    ) -> PendingPostSendSubmission | None:
        with self._post_send_pending_lock:
            return self._post_send_pending.pop(intent_id, None)

    def _handle_post_send_success(self, outcome: CaptureOutcome) -> None:
        pending = self._pop_post_send_pending(outcome.intent_id)
        if pending is None:
            return
        try:
            current_app_name, current_bundle_id, current_pid = get_current_app_target()
        except Exception:
            current_app_name, current_bundle_id, current_pid = (
                "Unknown",
                "unknown",
                -1,
            )
        if current_pid <= 0 or (
            current_pid != pending.target_pid or current_bundle_id != pending.bundle_id
        ):
            self._emit_capture_diagnostic(
                pending.app_name,
                pending.bundle_id,
                event_type=pending.event_type,
                decision_action="skip",
                decision_reason="post_send_target_changed",
                physical_key_count=pending.physical_key_count,
                diagnostics={"intent_id": pending.intent_id},
            )
            return
        if (
            pending.baseline_window_id is None
            or outcome.window_id != pending.baseline_window_id
        ):
            self._emit_capture_diagnostic(
                pending.app_name,
                pending.bundle_id,
                event_type=pending.event_type,
                decision_action="skip",
                decision_reason="post_send_window_changed",
                physical_key_count=pending.physical_key_count,
                diagnostics={"intent_id": pending.intent_id},
            )
            return
        if (
            not session_anchors_match(
                pending.session_anchor,
                outcome.session_anchor,
            )
        ):
            self._emit_capture_diagnostic(
                pending.app_name,
                pending.bundle_id,
                event_type=pending.event_type,
                decision_action="skip",
                decision_reason="post_send_session_changed",
                physical_key_count=pending.physical_key_count,
                diagnostics={"intent_id": pending.intent_id},
            )
            return
        self._emit_submission_event(
            app_name=pending.app_name,
            bundle_id=pending.bundle_id,
            content=outcome.content,
            key_modifiers=pending.key_modifiers,
            context_data={},
            fallback_source=outcome.source,
            physical_key_count=pending.physical_key_count,
        )

    def _handle_post_send_failure(self, outcome: CaptureOutcome) -> None:
        pending = self._pop_post_send_pending(outcome.intent_id)
        if pending is None:
            return
        diagnostic_details = {"intent_id": pending.intent_id}
        if outcome.diagnostics:
            diagnostic_details["source_diagnostics"] = list(outcome.diagnostics)
        self._emit_capture_diagnostic(
            pending.app_name,
            pending.bundle_id,
            event_type=pending.event_type,
            decision_action="skip",
            decision_reason=outcome.failure_reason or "post_send_failed",
            physical_key_count=pending.physical_key_count,
            diagnostics=diagnostic_details,
        )

    def _emit_submission_snapshot(
        self,
        event,
        app_name: str | None = None,
        bundle_id: str | None = None,
        key_modifiers: dict | None = None,
        event_type=None,
        target_pid: int | None = None,
        captured_context=None,
    ):
        """Emit the full focused input value when Enter is pressed."""
        if app_name is None or bundle_id is None:
            app_name, bundle_id = self._get_event_target_app(event)
        key_modifiers = key_modifiers or {
            "shift": False,
            "ctrl": False,
            "alt": False,
            "cmd": False,
        }
        context = (
            captured_context
            if captured_context is not None
            else self._capture_focused_context(target_pid=target_pid)
        )
        captured_context_data = self._context_to_dict_safe(context)
        context_data = captured_context_data if config.capture_context_on_enter else {}
        current_field_id = focused_field_identity(context)
        capture_status = getattr(
            context, "capture_status", None
        ) or captured_context_data.get("capture_status", "ok")
        if is_secure_text_entry_context(context):
            physical_key_count = self._pop_fallback_count(
                app_name, bundle_id, current_field_id=current_field_id
            )
            self._clear_submission_buffers(app_name, bundle_id)
            self._emit_capture_diagnostic(
                app_name,
                bundle_id,
                event_type=event_type,
                decision_action="skip",
                decision_reason="secure_text_input",
                physical_key_count=physical_key_count,
                context_data=captured_context_data,
            )
            return

        if capture_status != "ok":
            physical_key_count = self._pop_fallback_count(
                app_name, bundle_id, current_field_id=current_field_id
            )
            self._clear_submission_buffers(app_name, bundle_id)
            self._emit_capture_diagnostic(
                app_name,
                bundle_id,
                event_type=event_type,
                decision_action="skip",
                decision_reason="degraded_context",
                physical_key_count=physical_key_count,
                context_data=captured_context_data,
            )
            return

        if _enter_is_editor_newline(app_name, bundle_id, context):
            physical_key_count = self._pop_fallback_count(
                app_name, bundle_id, current_field_id=current_field_id
            )
            self._clear_submission_buffers(app_name, bundle_id)
            self._emit_capture_diagnostic(
                app_name,
                bundle_id,
                event_type=event_type,
                decision_action="skip",
                decision_reason="editor_newline",
                physical_key_count=physical_key_count,
                context_data=captured_context_data,
            )
            return

        text_entry_context = is_text_entry_context(context)
        self._active_field_ids[self._fallback_buffer_key(app_name, bundle_id)] = (
            current_field_id
        )
        ax_content = ""
        if text_entry_context:
            focused_value = getattr(context, "focused_value", None)
            if not isinstance(focused_value, str):
                focused_value = self._get_focused_text_snapshot()
            ax_content = normalize_submission_text(
                focused_value,
                app_name=app_name,
                bundle_id=bundle_id,
            )
            if len(ax_content) > MAX_TRUSTED_SUBMISSION_CHARS:
                physical_key_count = self._pop_fallback_count(
                    app_name, bundle_id, current_field_id=current_field_id
                )
                self._clear_submission_buffers(app_name, bundle_id)
                self._emit_capture_diagnostic(
                    app_name,
                    bundle_id,
                    event_type=event_type,
                    decision_action="skip",
                    decision_reason="suspected_whole_document",
                    selected_source="ax_value",
                    physical_key_count=physical_key_count,
                    context_data=captured_context_data,
                    diagnostics={"candidate_char_count": len(ax_content)},
                )
                return
        content = ax_content
        char_count_override = None
        redacted_content = False
        fallback_source = None
        physical_key_count = None
        capture_diagnostics = None

        if bundle_id == "com.apple.Safari":
            # Do not persist text or AX element identity in diagnostics.
            snapshot = self._recent_text_snapshots.get(
                self._fallback_buffer_key(app_name, bundle_id)
            )
            snapshot_field_id = (
                snapshot[2] if snapshot is not None and len(snapshot) > 2 else None
            )
            capture_diagnostics = {
                "verified_text_entry": bool(text_entry_context),
                "ax_value_nonempty": bool(ax_content),
                "snapshot_available": snapshot is not None,
                "snapshot_same_field": bool(
                    current_field_id is not None
                    and snapshot_field_id is not None
                    and current_field_id == snapshot_field_id
                ),
                "ax_range_attempted": False,
                "ax_range_available": False,
            }
            if text_entry_context and not content and target_pid and target_pid > 0:
                capture_diagnostics["ax_range_attempted"] = True
                range_content = normalize_submission_text(
                    read_focused_text_via_range(
                        target_pid, max_chars=MAX_TRUSTED_SUBMISSION_CHARS
                    ),
                    app_name=app_name,
                    bundle_id=bundle_id,
                )
                if range_content:
                    content = range_content
                    fallback_source = "ax_range"
                    capture_diagnostics["ax_range_available"] = True
        if (
            text_entry_context
            and fallback_source != "doubao_candidate_text"
            and (not content or not _contains_cjk(content))
        ):
            key_event_content = normalize_submission_text(
                self._pop_text_fallback_content(
                    app_name,
                    bundle_id,
                    current_field_id=current_field_id,
                ),
                app_name=app_name,
                bundle_id=bundle_id,
            )
            if key_event_content:
                content = key_event_content
                fallback_source = "key_event_text"
        if text_entry_context and not content:
            content = self._pop_recent_text_snapshot_content(
                app_name,
                bundle_id,
                current_field_id=current_field_id,
            )
            if content:
                fallback_source = "recent_ax_snapshot"
        if not content:
            if physical_key_count is None:
                physical_key_count = self._pop_fallback_count(
                    app_name, bundle_id, current_field_id=current_field_id
                )
            if not text_entry_context:
                self._clear_submission_buffers(app_name, bundle_id)
                self._emit_capture_diagnostic(
                    app_name,
                    bundle_id,
                    event_type=event_type,
                    decision_action="skip",
                    decision_reason="focused_element_not_text_input",
                    physical_key_count=physical_key_count,
                    context_data=captured_context_data,
                    diagnostics={
                        "trusted_text_entry_context": False,
                    },
                )
                now = time.monotonic()
                if now - self._last_empty_submission_log >= 5.0:
                    role = context_data.get("focused_role") or "unknown"
                    print(
                        f"⚠️  Enter 提交未保存：焦点不是文本输入控件 ({role}) -> {app_name} ({bundle_id})"
                    )
                    self._last_empty_submission_log = now
                return
            fallback_count = (
                physical_key_count
                if getattr(config, "count_unreadable_submissions", True)
                else 0
            )
            if content:
                self._clear_submission_buffers(app_name, bundle_id)
            elif fallback_count > 0:
                content = UNREADABLE_SUBMISSION_PLACEHOLDER
                char_count_override = fallback_count
                redacted_content = True
                fallback_source = "count_unreadable"
            else:
                self._clear_submission_buffers(app_name, bundle_id)
                self._emit_capture_diagnostic(
                    app_name,
                    bundle_id,
                    event_type=event_type,
                    decision_action="skip",
                    decision_reason="no_trusted_content",
                    physical_key_count=physical_key_count,
                    context_data=captured_context_data,
                    diagnostics={
                        "count_unreadable_enabled": getattr(
                            config, "count_unreadable_submissions", True
                        ),
                    },
                )
                now = time.monotonic()
                if now - self._last_empty_submission_log >= 5.0:
                    print(
                        f"⚠️  Enter 提交未保存：无法读取输入框文本 -> {app_name} ({bundle_id})"
                    )
                    self._last_empty_submission_log = now
                return
        else:
            self._clear_submission_buffers(app_name, bundle_id)

        if redacted_content:
            now = time.monotonic()
            if now - self._last_empty_submission_log >= 5.0:
                print(
                    f"⚠️  Enter 提交使用计数降级：{char_count_override} chars -> {app_name} ({bundle_id})"
                )
                self._last_empty_submission_log = now
        elif fallback_source == "key_event_text":
            now = time.monotonic()
            if now - self._last_empty_submission_log >= 5.0:
                print(
                    f"⚠️  Enter 提交使用键盘文本降级：{len(content)} chars -> {app_name} ({bundle_id})"
                )
                self._last_empty_submission_log = now

        if _DEBUG:
            print(f"[DEBUG] Enter 提交快照: {len(content)} chars -> {app_name}")

        self._emit_submission_event(
            app_name=app_name,
            bundle_id=bundle_id,
            content=content,
            key_modifiers=key_modifiers,
            context_data=context_data,
            fallback_source=fallback_source,
            char_count_override=char_count_override,
            redacted_content=redacted_content,
            physical_key_count=physical_key_count,
            capture_diagnostics=capture_diagnostics,
        )

    def _resolve_raw_event_target(
        self,
        raw_event: RawKeyboardEvent,
    ) -> tuple[str, str, int]:
        sampled_identity = (raw_event.app_name, raw_event.bundle_id)
        if (
            raw_event.bundle_id in SUPPORTED_TARGET_BUNDLE_IDS
            and raw_event.frontmost_pid > 0
            and self._target_app_identities.get(raw_event.frontmost_pid)
            == sampled_identity
        ):
            return (*sampled_identity, raw_event.frontmost_pid)
        if raw_event.target_pid > 0:
            app_name, bundle_id = get_app_by_pid(raw_event.target_pid)
            return app_name, bundle_id, raw_event.target_pid
        return raw_event.app_name, raw_event.bundle_id, 0

    def _process_raw_event(self, raw_event: RawKeyboardEvent):
        """Process a sampled key event outside the EventTap callback thread."""
        event_type = raw_event.event_type
        keycode = raw_event.keycode
        app_name, bundle_id, application_pid = self._resolve_raw_event_target(raw_event)
        if application_pid > 0:
            if len(self._target_app_identities) >= 32:
                self._target_app_identities.clear()
            self._target_app_identities[application_pid] = (
                app_name,
                bundle_id,
            )
        self._update_doubao_target(
            app_name,
            bundle_id,
            application_pid,
        )
        if config.is_app_ignored(bundle_id):
            self._clear_submission_buffers(app_name, bundle_id)
            self._active_field_ids.pop(
                self._fallback_buffer_key(app_name, bundle_id), None
            )
            return
        set_last_input_app(app_name, bundle_id)
        modifiers = raw_event.modifiers

        if event_type in (
            kCGEventLeftMouseDown,
            kCGEventRightMouseDown,
            kCGEventOtherMouseDown,
        ):
            self._clear_submission_buffers(app_name, bundle_id)
            return

        if (
            bundle_id in SUPPORTED_TARGET_BUNDLE_IDS
            and event_type == kCGEventKeyDown
            and (
                modifiers.get("cmd")
                or modifiers.get("ctrl")
                or modifiers.get("alt")
                or keycode in (48, 53, 117)
            )
        ):
            if (
                bundle_id in POST_SEND_CHAT_BUNDLE_IDS
                and keycode == 9
                and modifiers.get("cmd")
                and not modifiers.get("ctrl")
                and not modifiers.get("alt")
            ):
                paste_is_standalone = self._tracked_post_send_composer_is_empty(
                    app_name,
                    bundle_id,
                )
                self._schedule_post_send_baseline(
                    app_name,
                    bundle_id,
                    application_pid,
                )
                self._clear_submission_buffers(app_name, bundle_id)
                paste_key = self._fallback_buffer_key(app_name, bundle_id)
                self._post_send_paste_snapshot_keys.add(paste_key)
                if paste_is_standalone:
                    self._pending_post_send_paste_keys.add(paste_key)
                return
            self._clear_submission_buffers(app_name, bundle_id)
            return

        if event_type == kCGEventKeyUp and keycode != ENTER_KEYCODE:
            if bundle_id not in POST_SEND_CHAT_BUNDLE_IDS or (
                bundle_id in POST_SEND_CHAT_BUNDLE_IDS
                and keycode == 9
                and modifiers.get("cmd")
                and not modifiers.get("ctrl")
                and not modifiers.get("alt")
            ):
                self._record_recent_text_snapshot(
                    app_name,
                    bundle_id,
                    clear_on_empty=keycode in (51, 117),
                    target_pid=application_pid,
                )
                if bundle_id in POST_SEND_CHAT_BUNDLE_IDS and keycode == 9:
                    self._record_post_send_paste_validation(app_name, bundle_id)
            self._record_text_fallback_key(
                app_name,
                bundle_id,
                keycode,
                modifiers,
                raw_event.text,
                track_editing_keys=False,
            )
            if bundle_id not in POST_SEND_CHAT_BUNDLE_IDS:
                self._refresh_doubao_candidates(
                    app_name,
                    bundle_id,
                    application_pid,
                    keycode,
                    modifiers,
                )
        if event_type == kCGEventKeyDown and keycode != ENTER_KEYCODE:
            if bundle_id in POST_SEND_CHAT_BUNDLE_IDS:
                key = self._fallback_buffer_key(app_name, bundle_id)
                if key in self._post_send_paste_snapshot_keys:
                    self._tainted_post_send_validation_keys.add(key)
                    self._clear_recent_text_snapshot(app_name, bundle_id)
                self._recent_paste_validations.pop(key, None)
                self._pending_post_send_paste_keys.discard(key)
            self._record_text_fallback_key(
                app_name,
                bundle_id,
                keycode,
                modifiers,
                raw_event.text,
            )
            self._record_fallback_key(
                app_name,
                bundle_id,
                keycode,
                modifiers,
                raw_event.text,
            )
            self._handle_doubao_keydown(
                app_name,
                bundle_id,
                application_pid,
                keycode,
                raw_event.text,
                modifiers,
                raw_event.is_autorepeat,
            )

        if (
            bundle_id in POST_SEND_CHAT_BUNDLE_IDS
            and event_type == kCGEventKeyDown
            and keycode != ENTER_KEYCODE
            and not raw_event.is_autorepeat
            and not any(modifiers.values())
        ):
            self._schedule_post_send_baseline(
                app_name,
                bundle_id,
                application_pid,
            )

        if keycode != ENTER_KEYCODE or event_type not in (
            kCGEventKeyDown,
            kCGEventKeyUp,
        ):
            return
        if (
            bundle_id in POST_SEND_CHAT_BUNDLE_IDS
            and self._post_send_coordinator is not None
        ):
            if event_type != kCGEventKeyDown or raw_event.is_autorepeat:
                return
            if modifiers.get("shift") or modifiers.get("alt"):
                self._record_post_send_newline(app_name, bundle_id)
                self._record_recent_text_snapshot(app_name, bundle_id)
                self._emit_capture_diagnostic(
                    app_name,
                    bundle_id,
                    event_type=event_type,
                    decision_action="skip",
                    decision_reason="newline_modifier",
                )
                return
            if modifiers.get("cmd") or modifiers.get("ctrl"):
                self._emit_capture_diagnostic(
                    app_name,
                    bundle_id,
                    event_type=event_type,
                    decision_action="skip",
                    decision_reason="shortcut_modifier",
                )
                return
            if application_pid <= 0:
                self._clear_submission_buffers(app_name, bundle_id)
                self._emit_capture_diagnostic(
                    app_name,
                    bundle_id,
                    event_type=event_type,
                    decision_action="skip",
                    decision_reason="post_send_target_unverified",
                )
                return
            self._process_post_send_chat_enter(
                raw_event,
                app_name,
                bundle_id,
                application_pid,
            )
            return
        attempt_key = self._fallback_buffer_key(app_name, bundle_id)
        if event_type == kCGEventKeyDown:
            if raw_event.is_autorepeat:
                return
            self._pending_enter_keyups.discard(attempt_key)
            self._ignore_enter_keyup_once(app_name, bundle_id)
        if self._should_ignore_enter_keyup(app_name, bundle_id, event_type):
            return

        if modifiers.get("shift") or modifiers.get("alt"):
            self._emit_capture_diagnostic(
                app_name,
                bundle_id,
                event_type=event_type,
                decision_action="skip",
                decision_reason="newline_modifier",
            )
        elif modifiers.get("cmd") or modifiers.get("ctrl"):
            self._emit_capture_diagnostic(
                app_name,
                bundle_id,
                event_type=event_type,
                decision_action="skip",
                decision_reason="shortcut_modifier",
            )
        else:
            candidate_result = self._handle_doubao_keydown(
                app_name,
                bundle_id,
                application_pid,
                keycode,
                raw_event.text,
                modifiers,
                raw_event.is_autorepeat,
            )
            if candidate_result is not None and candidate_result.candidate_committed:
                self._emit_capture_diagnostic(
                    app_name,
                    bundle_id,
                    event_type=event_type,
                    decision_action="skip",
                    decision_reason="ime_candidate_commit",
                    selected_source="doubao_candidate_ax",
                    selected_confidence=0.9,
                )
                return
            self._emit_submission_snapshot(
                None,
                app_name=app_name,
                bundle_id=bundle_id,
                key_modifiers=modifiers,
                event_type=event_type,
                target_pid=application_pid,
            )

    def _event_worker_loop(self):
        while self._event_worker_running or not self._event_queue.empty():
            try:
                raw_event = self._event_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                if raw_event is None:
                    return
                with self._event_processing_lock:
                    if self._has_started and not self._running:
                        continue
                    self._process_raw_event(raw_event)
            except Exception as e:
                record_runtime_error(f"event_worker_failed:{e}")
                if _DEBUG:
                    print(f"[DEBUG] queued keyboard event failed: {e}")
            finally:
                self._event_queue.task_done()

    def _event_callback(self, proxy, event_type, event, refcon):
        """Sample the native event quickly and return control to macOS."""
        if event_type in (
            EVENT_TAP_DISABLED_BY_TIMEOUT,
            EVENT_TAP_DISABLED_BY_USER_INPUT,
        ):
            if self._tap is not None:
                CGEventTapEnable(self._tap, True)
            return event
        if event_type in (
            kCGEventKeyDown,
            kCGEventKeyUp,
            kCGEventFlagsChanged,
            kCGEventLeftMouseDown,
            kCGEventRightMouseDown,
            kCGEventOtherMouseDown,
        ):
            try:
                target_pid = self._get_event_target_pid(event)
                if self._has_started:
                    app_name, bundle_id, frontmost_pid = get_current_app_target()
                else:
                    app_name, bundle_id = self._get_event_target_app(event)
                    frontmost_pid = target_pid
                is_mouse_event = event_type in (
                    kCGEventLeftMouseDown,
                    kCGEventRightMouseDown,
                    kCGEventOtherMouseDown,
                )
                keycode = (
                    0
                    if is_mouse_event
                    else CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode)
                )
                modifiers = self._event_modifiers(event)
                is_autorepeat = bool(
                    CGEventGetIntegerValueField(
                        event,
                        KEYBOARD_EVENT_AUTOREPEAT_FIELD,
                    )
                )
                raw_event = RawKeyboardEvent(
                    event_type=event_type,
                    keycode=keycode,
                    text="" if is_mouse_event else self._get_keyboard_event_text(event),
                    app_name=app_name,
                    bundle_id=bundle_id,
                    modifiers=modifiers,
                    target_pid=target_pid,
                    frontmost_pid=frontmost_pid,
                    is_autorepeat=is_autorepeat,
                    occurred_at=time.monotonic(),
                    timestamp=storage_now(),
                )
                if self._event_worker_running:
                    try:
                        self._event_queue.put_nowait(raw_event)
                    except queue.Full:
                        self._dropped_event_count += 1
                elif not self._has_started:
                    self._process_raw_event(raw_event)
                else:
                    self._dropped_event_count += 1
            except Exception:
                self._dropped_event_count += 1

        return event

    def _create_event_tap(self) -> bool:
        """创建 CGEventTap，返回是否成功"""
        with self._tap_lock:
            # 清理旧的 tap
            if self._tap is not None:
                try:
                    CGEventTapEnable(self._tap, False)
                except:
                    pass
                self._tap = None

            event_mask = (
                (1 << kCGEventKeyDown)
                | (1 << kCGEventKeyUp)
                | (1 << kCGEventFlagsChanged)
                | (1 << kCGEventLeftMouseDown)
                | (1 << kCGEventRightMouseDown)
                | (1 << kCGEventOtherMouseDown)
            )

            self._tap = CGEventTapCreate(
                kCGSessionEventTap,
                kCGHeadInsertEventTap,
                0,
                event_mask,
                self._event_callback,
                None,
            )

            if self._tap is None:
                print("❌ 无法创建 CGEventTap")
                print("请确保已授予辅助功能权限")
                set_recording_status("error", error="unable_to_create_cgeventtap")
                return False

            return True

    def _is_tap_healthy(self) -> bool:
        """检查 CGEventTap 是否健康"""
        with self._tap_lock:
            if self._tap is None:
                return False
            try:
                # 检查 MachPort 是否有效
                if not CFMachPortIsValid(self._tap):
                    print("⚠️  CGEventTap MachPort 无效")
                    return False
                # 检查 tap 是否启用
                if not CGEventTapIsEnabled(self._tap):
                    print("⚠️  CGEventTap 已被禁用，尝试重新启用...")
                    CGEventTapEnable(self._tap, True)
                    # 再次检查
                    if not CGEventTapIsEnabled(self._tap):
                        print("❌ 无法重新启用 CGEventTap")
                        return False
                    print("✅ CGEventTap 已重新启用")
                return True
            except Exception as e:
                print(f"⚠️  检查 CGEventTap 状态失败: {e}")
                return False

    def _health_check_loop(self):
        """健康检查循环"""
        while self._running:
            time.sleep(self.HEALTH_CHECK_INTERVAL)
            if not self._running:
                break

            refresh_runtime_heartbeat()

            dropped_event_count = self._dropped_event_count
            if dropped_event_count:
                self._dropped_event_count = 0
                record_runtime_error(f"event_queue_dropped:{dropped_event_count}")
                print(f"⚠️  键盘事件队列已丢弃 {dropped_event_count} 个采样事件")

            if not self._is_tap_healthy():
                print("🔄 CGEventTap 不健康，尝试重建...")
                self._rebuild_tap()

    def _rebuild_tap(self):
        """重建 CGEventTap"""
        if self._retry_count >= self.MAX_RETRY_COUNT:
            print(f"❌ 已达到最大重试次数 ({self.MAX_RETRY_COUNT})，停止重试")
            set_recording_status("error", error="cgeventtap_max_retries_reached")
            return

        self._retry_count += 1
        print(f"🔄 第 {self._retry_count} 次尝试重建 CGEventTap...")

        with self._tap_lock:
            # 移除旧的 source
            if self._run_loop_source and self._run_loop:
                try:
                    CFRunLoopRemoveSource(
                        self._run_loop,
                        self._run_loop_source,
                        Quartz.kCFRunLoopCommonModes,
                    )
                except:
                    pass
                self._run_loop_source = None

        # 创建新的 tap
        if self._create_event_tap():
            with self._tap_lock:
                self._run_loop_source = CFMachPortCreateRunLoopSource(
                    None, self._tap, 0
                )
                if self._run_loop:
                    CFRunLoopAddSource(
                        self._run_loop,
                        self._run_loop_source,
                        Quartz.kCFRunLoopCommonModes,
                    )
                    CGEventTapEnable(self._tap, True)
                    print("✅ CGEventTap 重建成功")
                    set_recording_status("recording")
                    self._retry_count = 0  # 重置重试计数
        else:
            print("❌ CGEventTap 重建失败")
            set_recording_status("error", error="cgeventtap_rebuild_failed")

    def _on_system_wake(self, notification):
        """系统唤醒回调"""
        print("💤 检测到系统唤醒，检查 CGEventTap 状态...")

        # 延迟一下再检查，等系统完全唤醒
        def delayed_check():
            time.sleep(2)
            if self._running and not self._is_tap_healthy():
                print("🔄 系统唤醒后 CGEventTap 失效，尝试重建...")
                self._rebuild_tap()
            else:
                print("✅ 系统唤醒后 CGEventTap 状态正常")

        threading.Thread(target=delayed_check, daemon=True).start()

    def _start_wake_observer(self):
        """启动系统唤醒事件监听"""
        try:
            ws = NSWorkspace.sharedWorkspace()
            nc = ws.notificationCenter()

            # 创建观察者类
            class WakeObserver(NSObject):
                def init(self_inner):
                    self_inner = objc.super(WakeObserver, self_inner).init()
                    return self_inner

                def onWake_(self_inner, notification):
                    self._on_system_wake(notification)

            self._wake_observer = WakeObserver.alloc().init()

            # 监听系统唤醒事件
            nc.addObserver_selector_name_object_(
                self._wake_observer,
                objc.selector(self._wake_observer.onWake_, signature=b"v@:@"),
                "NSWorkspaceDidWakeNotification",
                None,
            )
            print("👁️  系统唤醒监听已启动")
        except Exception as e:
            print(f"⚠️  启动系统唤醒监听失败: {e}")

    def _run_loop_thread(self):
        if not self._create_event_tap():
            return

        with self._tap_lock:
            self._run_loop_source = CFMachPortCreateRunLoopSource(None, self._tap, 0)
            self._run_loop = CFRunLoopGetCurrent()
            CFRunLoopAddSource(
                self._run_loop, self._run_loop_source, Quartz.kCFRunLoopCommonModes
            )
            CGEventTapEnable(self._tap, True)

        print("✅ 键盘监听已启动")
        print("📝 Enter 提交快照监听已启动")
        set_recording_status("recording")

        # 使用带超时的循环代替 CFRunLoopRun()，
        # 避免 Rosetta 翻译下 CGEventTap 回调导致忙循环
        while self._running:
            Quartz.CFRunLoopRunInMode(Quartz.kCFRunLoopDefaultMode, 1.0, False)

    def start(self):
        if self._running:
            return
        if self._event_worker_thread and self._event_worker_thread.is_alive():
            raise RuntimeError("previous keyboard event worker is still stopping")
        self._retry_count = 0
        self._ensure_post_send_services()
        self._last_warmed_post_send_target = None

        # 首次前台应用快照与窗口预备完成后，才开放键盘事件入口。
        _start_app_watcher(self._prepare_activated_composer)

        self._event_queue = queue.Queue(maxsize=EVENT_QUEUE_MAX_SIZE)
        self._has_started = True
        self._running = True
        self._event_worker_running = True
        self._event_worker_thread = threading.Thread(
            target=self._event_worker_loop, daemon=True
        )
        self._event_worker_thread.start()

        # 启动系统唤醒监听
        self._start_wake_observer()

        # 启动键盘监听
        self._thread = threading.Thread(target=self._run_loop_thread, daemon=True)
        self._thread.start()

        # 启动健康检查线程
        self._health_check_thread = threading.Thread(
            target=self._health_check_loop, daemon=True
        )
        self._health_check_thread.start()

        # 提交快照模式不启动 Rime 日志监听，避免保存拼音中间态。

    def stop(self):
        if not self._running:
            return
        _set_app_activation_callback(None)
        with self._tap_lock:
            if self._tap:
                try:
                    CGEventTapEnable(self._tap, False)
                except:
                    pass
            if self._run_loop:
                CFRunLoopStop(self._run_loop)

        with self._event_processing_lock:
            self._running = False
            self._event_worker_running = False
        if self._owns_post_send_coordinator and self._post_send_coordinator is not None:
            self._post_send_coordinator.stop()
            self._post_send_coordinator = None
        if self._owns_baseline_sampler and self._baseline_sampler is not None:
            self._baseline_sampler.stop()
            self._baseline_sampler = None
        with self._post_send_pending_lock:
            self._post_send_pending.clear()
        self._recent_paste_validations.clear()
        self._pending_post_send_paste_keys.clear()
        self._post_send_paste_snapshot_keys.clear()
        self._tainted_post_send_validation_keys.clear()
        while True:
            try:
                self._event_queue.get_nowait()
            except queue.Empty:
                break
            else:
                self._event_queue.task_done()
        self._event_queue.put_nowait(None)
        self._rime_watcher.stop()

        if self._health_check_thread:
            self._health_check_thread.join(timeout=1.0)
        if self._thread:
            self._thread.join(timeout=1.0)
        if self._event_worker_thread:
            self._event_worker_thread.join(timeout=1.0)

        # 移除唤醒监听
        if self._wake_observer:
            try:
                ws = NSWorkspace.sharedWorkspace()
                nc = ws.notificationCenter()
                nc.removeObserver_(self._wake_observer)
            except:
                pass
            self._wake_observer = None

        print("⏹️ 监听已停止")
        set_recording_status("paused")

    def is_running(self) -> bool:
        return self._running


def check_accessibility_permission() -> bool:
    from ApplicationServices import AXIsProcessTrusted

    return AXIsProcessTrusted()


def request_accessibility_permission():
    from ApplicationServices import AXIsProcessTrustedWithOptions
    from Foundation import NSDictionary

    options = NSDictionary.dictionaryWithObject_forKey_(
        True, "AXTrustedCheckOptionPrompt"
    )
    AXIsProcessTrustedWithOptions(options)


if __name__ == "__main__":
    print("🔍 检查辅助功能权限...")

    if not check_accessibility_permission():
        print("❌ 没有辅助功能权限，正在请求...")
        request_accessibility_permission()
        print("请在系统偏好设置中授予权限后重新运行")
        exit(1)

    print("✅ 已获得辅助功能权限")
    print("-" * 50)

    last_app = [""]

    def on_key(event: KeyEvent):
        if last_app[0] != event.app_name:
            if last_app[0]:
                print()
            print(f"\n[{event.app_name}] ", end="", flush=True)
            last_app[0] = event.app_name

        char = event.character
        if event.modifiers.get("submit_snapshot"):
            print(
                f"\033[32m{format_submission_terminal_notice(char)}\033[0m", flush=True
            )
        elif char == "\n":
            print()
            print(f"[{event.app_name}] ", end="", flush=True)
        elif char == "\b":
            print("\b \b", end="", flush=True)
        elif char in ["esc", "←", "→", "↑", "↓", "del"] or (
            len(char) <= 3 and char.startswith("F")
        ):
            pass
        else:
            if event.is_ime_input:
                print(f"\033[32m{char}\033[0m", end="", flush=True)
            else:
                print(f"{char}", end="", flush=True)

    listener = KeyboardListener(on_key)
    listener.start()

    print("\n按 Ctrl+C 停止监听...")
    print("💡 中文显示为绿色\n")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        listener.stop()
        print("\n已停止")
