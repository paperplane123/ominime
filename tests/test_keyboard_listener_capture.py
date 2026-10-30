import importlib
from pathlib import Path
import sys
import threading
import time
import types
from types import SimpleNamespace

import pytest

from ominime.ime_candidate_capture import CandidateSnapshot


def test_no_chat_presubmit_interception_symbols_remain_in_production():
    root = Path(__file__).parents[1]
    listener_source = (root / "src/ominime/keyboard_listener.py").read_text()
    kim_source = (root / "src/ominime/kim_composer_capture.py").read_text()
    wechat_source = (root / "src/ominime/wechat_composer_capture.py").read_text()

    for forbidden in (
        "PendingReplay",
        "ENTER_REPLAY_TIMEOUT_SECONDS",
        "_freeze_presubmit_composer",
        "_create_pending_replay",
    ):
        assert forbidden not in listener_source
    assert "KIM_COMPOSER_ROI" not in kim_source
    assert "WECHAT_COMPOSER_ROI" not in wechat_source


def import_keyboard_listener(monkeypatch):
    quartz = types.ModuleType("Quartz")
    quartz.kCGEventKeyDown = 1
    quartz.kCGEventKeyUp = 2
    quartz.kCGEventFlagsChanged = 3
    quartz.kCGEventLeftMouseDown = 4
    quartz.kCGEventRightMouseDown = 5
    quartz.kCGEventOtherMouseDown = 6
    quartz.kCGEventTapDisabledByTimeout = 90
    quartz.kCGEventTapDisabledByUserInput = 91
    quartz.kCGKeyboardEventKeycode = 4
    quartz.kCGKeyboardEventAutorepeat = 5
    quartz.kCGSessionEventTap = 5
    quartz.kCGHeadInsertEventTap = 6
    quartz.kCGEventFlagMaskShift = 1 << 17
    quartz.kCGEventFlagMaskControl = 1 << 18
    quartz.kCGEventFlagMaskAlternate = 1 << 19
    quartz.kCGEventFlagMaskCommand = 1 << 20
    quartz.kCFRunLoopDefaultMode = "default"
    quartz.kCGEventSourceUserData = 42
    quartz.posted_events = []

    for name in (
        "CGEventTapEnable",
        "CGEventTapIsEnabled",
        "CFMachPortIsValid",
        "CFMachPortCreateRunLoopSource",
        "CFRunLoopAddSource",
        "CFRunLoopRemoveSource",
        "CFRunLoopGetCurrent",
        "CFRunLoopRun",
        "CFRunLoopStop",
        "CFRunLoopRunInMode",
        "CGEventGetFlags",
    ):
        setattr(quartz, name, lambda *args, **kwargs: None)
    quartz.CGEventGetFlags = lambda event: getattr(event, "flags", 0)

    captured = {}

    def fake_event_tap_create(*args):
        captured["event_mask"] = args[3]
        return object()

    quartz.CGEventTapCreate = fake_event_tap_create
    quartz.CGEventGetIntegerValueField = lambda event, field: (
        getattr(event, "user_data", 0)
        if field == quartz.kCGEventSourceUserData
        else (
            getattr(event, "target_pid", 0)
            if field == 40
            else (
                getattr(event, "autorepeat", 0)
                if field == quartz.kCGKeyboardEventAutorepeat
                else getattr(event, "keycode", 0)
            )
        )
    )
    quartz.CGEventCreateCopy = lambda event: SimpleNamespace(**vars(event))
    quartz.CGEventSetIntegerValueField = lambda event, field, value: setattr(
        event, "user_data", value
    )
    quartz.CGEventSetType = lambda event, event_type: setattr(
        event, "event_type", event_type
    )
    quartz.CGEventPostToPid = lambda pid, event: quartz.posted_events.append(
        (pid, event)
    )
    quartz.CGEventKeyboardGetUnicodeString = (
        lambda event, max_length, actual_length, chars: (
            len(getattr(event, "text", "")),
            getattr(event, "text", ""),
        )
    )
    monkeypatch.setitem(sys.modules, "Quartz", quartz)

    appkit = types.ModuleType("AppKit")
    appkit.NSWorkspace = SimpleNamespace(sharedWorkspace=lambda: None)
    appkit.NSRunningApplication = SimpleNamespace
    monkeypatch.setitem(sys.modules, "AppKit", appkit)

    foundation = types.ModuleType("Foundation")
    foundation.NSObject = object
    foundation.NSRunLoop = SimpleNamespace
    foundation.NSDefaultRunLoopMode = "NSDefaultRunLoopMode"
    foundation.NSDistributedNotificationCenter = SimpleNamespace
    monkeypatch.setitem(sys.modules, "Foundation", foundation)

    objc = types.ModuleType("objc")
    objc.selector = lambda value, signature=None: value
    monkeypatch.setitem(sys.modules, "objc", objc)

    sys.modules.pop("ominime.keyboard_listener", None)
    module = importlib.import_module("ominime.keyboard_listener")
    return module, captured


def test_event_tap_listens_to_keydown_keyup_and_flags_changed(monkeypatch):
    keyboard_listener, captured = import_keyboard_listener(monkeypatch)

    listener = keyboard_listener.KeyboardListener(lambda event: None)
    assert listener._create_event_tap()

    assert captured["event_mask"] == (
        (1 << keyboard_listener.kCGEventKeyDown)
        | (1 << keyboard_listener.kCGEventKeyUp)
        | (1 << keyboard_listener.kCGEventFlagsChanged)
        | (1 << keyboard_listener.kCGEventLeftMouseDown)
        | (1 << keyboard_listener.kCGEventRightMouseDown)
        | (1 << keyboard_listener.kCGEventOtherMouseDown)
    )


def test_disabled_event_tap_is_reenabled_immediately(monkeypatch):
    keyboard_listener, _ = import_keyboard_listener(monkeypatch)
    enabled = []
    monkeypatch.setattr(
        keyboard_listener,
        "CGEventTapEnable",
        lambda tap, value: enabled.append((tap, value)),
    )
    listener = keyboard_listener.KeyboardListener(lambda event: None)
    listener._tap = object()
    event = object()

    returned = listener._event_callback(
        None,
        keyboard_listener.EVENT_TAP_DISABLED_BY_TIMEOUT,
        event,
        None,
    )

    assert enabled == [(listener._tap, True)]
    assert returned is event


def test_running_event_tap_callback_only_enqueues_raw_event(monkeypatch):
    keyboard_listener, _ = import_keyboard_listener(monkeypatch)
    listener = keyboard_listener.KeyboardListener(lambda event: None)
    monkeypatch.setattr(
        keyboard_listener,
        "get_current_app_target",
        lambda: ("Codex", "com.openai.codex", 0),
    )
    listener._capture_focused_context = lambda: (_ for _ in ()).throw(
        AssertionError("EventTap callback must not read Accessibility")
    )
    listener._has_started = True
    listener._event_worker_running = True

    raw_event = SimpleNamespace(keycode=keyboard_listener.ENTER_KEYCODE, text="")
    returned = listener._event_callback(
        None,
        keyboard_listener.kCGEventKeyDown,
        raw_event,
        None,
    )

    queued = listener._event_queue.get_nowait()
    assert queued.event_type == keyboard_listener.kCGEventKeyDown
    assert queued.keycode == keyboard_listener.ENTER_KEYCODE
    assert queued.app_name == "Codex"
    assert returned is raw_event


class FakePostSendCoordinator:
    def __init__(self, *, accepts=True):
        self.accepts = accepts
        self.submitted = []
        self.stop_calls = 0

    def submit(self, intent):
        self.submitted.append(intent)
        if not self.accepts:
            release = getattr(intent.baseline, "release", None)
            if callable(release):
                release()
        return self.accepts

    def stop(self):
        self.stop_calls += 1


class FakeBaselineSampler:
    def __init__(self, baseline=None):
        if baseline is None:
            baseline = SimpleNamespace(
                window_id=42,
                session_anchor="session-a",
                release=lambda: None,
            )
        self.baseline = baseline
        self.scheduled = []
        self.taken = []
        self.take_waits = []
        self.stop_calls = 0

    def schedule(self, target_pid):
        self.scheduled.append(target_pid)
        return True

    def take_baseline(self, target_pid, wait_timeout=0.0):
        self.taken.append(target_pid)
        self.take_waits.append(wait_timeout)
        baseline = self.baseline
        self.baseline = None
        return baseline

    def stop(self):
        self.stop_calls += 1


def test_prepare_activated_chat_schedules_one_warm_baseline_per_activation(monkeypatch):
    keyboard_listener, _ = import_keyboard_listener(monkeypatch)
    sampler = FakeBaselineSampler()
    listener = keyboard_listener.KeyboardListener(
        lambda event: None,
        post_send_coordinator=FakePostSendCoordinator(),
        baseline_sampler=sampler,
    )

    listener._prepare_activated_composer("Kim", "Kem", 39522)
    listener._prepare_activated_composer("Kim", "Kem", 39522)

    assert listener._target_app_identities[39522] == ("Kim", "Kem")
    assert sampler.scheduled == [39522]

    listener._prepare_activated_composer("Notes", "com.apple.Notes", 777)
    listener._prepare_activated_composer("Kim", "Kem", 39522)

    assert sampler.scheduled == [39522, 39522]


def test_postsend_enter_waits_for_cold_native_baseline(monkeypatch):
    keyboard_listener, _ = import_keyboard_listener(monkeypatch)

    class ColdBaselineSampler(FakeBaselineSampler):
        def take_baseline(self, target_pid, wait_timeout=0.0):
            self.taken.append(target_pid)
            self.take_waits.append(wait_timeout)
            if wait_timeout < 0.5:
                return None
            return self.baseline

    sampler = ColdBaselineSampler()
    coordinator = FakePostSendCoordinator()
    listener = keyboard_listener.KeyboardListener(
        lambda event: None,
        post_send_coordinator=coordinator,
        baseline_sampler=sampler,
    )
    configure_listener_context(listener, status="degraded", secure=False)
    monkeypatch.setattr(keyboard_listener, "get_app_by_pid", lambda pid: ("Kim", "Kem"))

    listener._process_raw_event(
        keyboard_listener.RawKeyboardEvent(
            event_type=keyboard_listener.kCGEventKeyDown,
            keycode=keyboard_listener.ENTER_KEYCODE,
            text="",
            app_name="Kim",
            bundle_id="Kem",
            modifiers={"shift": False, "ctrl": False, "alt": False, "cmd": False},
            target_pid=39522,
        )
    )

    assert coordinator.submitted[0].baseline is sampler.baseline
    assert sampler.take_waits == [keyboard_listener.POST_SEND_BASELINE_WAIT_SECONDS]


@pytest.mark.parametrize(
    ("app_name", "bundle_id", "target_pid"),
    (("Kim", "Kem", 123), ("微信", "com.tencent.xinWeChat", 4318)),
)
def test_postsend_chat_enter_callback_returns_original_without_native_capture(
    monkeypatch,
    app_name,
    bundle_id,
    target_pid,
):
    keyboard_listener, _ = import_keyboard_listener(monkeypatch)
    coordinator = FakePostSendCoordinator()
    sampler = FakeBaselineSampler()
    listener = keyboard_listener.KeyboardListener(
        lambda event: None,
        post_send_coordinator=coordinator,
        baseline_sampler=sampler,
    )
    monkeypatch.setattr(
        keyboard_listener,
        "get_current_app_target",
        lambda: (app_name, bundle_id, target_pid),
    )
    monkeypatch.setattr(
        keyboard_listener.Quartz,
        "CGEventCreateCopy",
        lambda event: (_ for _ in ()).throw(
            AssertionError("chat Enter must not be copied")
        ),
    )
    listener._capture_focused_context = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("EventTap callback must not read AX")
    )
    listener._target_app_identities[target_pid] = (app_name, bundle_id)
    listener._has_started = True
    listener._event_worker_running = True
    native_event = SimpleNamespace(
        keycode=keyboard_listener.ENTER_KEYCODE,
        text="",
        target_pid=target_pid,
    )

    returned = listener._event_callback(
        None,
        keyboard_listener.kCGEventKeyDown,
        native_event,
        None,
    )

    queued = listener._event_queue.get_nowait()
    assert returned is native_event
    assert not hasattr(queued, "pending_replay")
    assert keyboard_listener.Quartz.posted_events == []
    assert coordinator.submitted == []
    assert sampler.scheduled == []


@pytest.mark.parametrize("event_type", (1, 2))
def test_postsend_chat_enter_keydown_and_keyup_are_passed_through(
    monkeypatch, event_type
):
    keyboard_listener, _ = import_keyboard_listener(monkeypatch)
    listener = keyboard_listener.KeyboardListener(
        lambda event: None,
        post_send_coordinator=FakePostSendCoordinator(),
        baseline_sampler=FakeBaselineSampler(),
    )
    monkeypatch.setattr(
        keyboard_listener,
        "get_current_app_target",
        lambda: ("Kim", "Kem", 123),
    )
    listener._has_started = True
    listener._event_worker_running = True
    native_event = SimpleNamespace(
        keycode=keyboard_listener.ENTER_KEYCODE,
        text="",
        target_pid=123,
    )

    returned = listener._event_callback(None, event_type, native_event, None)

    queued = listener._event_queue.get_nowait()
    assert returned is native_event
    assert queued.event_type == event_type
    assert keyboard_listener.Quartz.posted_events == []


def test_postsend_worker_creates_one_intent_after_chat_gates(monkeypatch):
    keyboard_listener, _ = import_keyboard_listener(monkeypatch)
    coordinator = FakePostSendCoordinator()
    sampler = FakeBaselineSampler()
    listener = keyboard_listener.KeyboardListener(
        lambda event: None,
        post_send_coordinator=coordinator,
        baseline_sampler=sampler,
    )
    configure_listener_context(listener, status="degraded", secure=False)
    monkeypatch.setattr(keyboard_listener, "get_app_by_pid", lambda pid: ("Kim", "Kem"))
    listener._fallback_buffers[("Kim", "Kem")] = ["a", "b"]
    listener._fallback_buffer_updated_at[("Kim", "Kem")] = time.monotonic()

    listener._process_raw_event(
        keyboard_listener.RawKeyboardEvent(
            event_type=keyboard_listener.kCGEventKeyDown,
            keycode=keyboard_listener.ENTER_KEYCODE,
            text="",
            app_name="Kim",
            bundle_id="Kem",
            modifiers={"shift": False, "ctrl": False, "alt": False, "cmd": False},
            target_pid=123,
            occurred_at=10.0,
        )
    )

    assert len(coordinator.submitted) == 1
    intent = coordinator.submitted[0]
    assert intent.target_pid == 123
    assert intent.app_name == "Kim"
    assert intent.physical_key_count == 2
    assert intent.baseline.session_anchor == "session-a"
    assert sampler.taken == [123]


@pytest.mark.parametrize("secure", (True, False))
def test_postsend_secure_or_count_only_chat_never_captures(
    monkeypatch,
    secure,
):
    keyboard_listener, _ = import_keyboard_listener(monkeypatch)
    coordinator = FakePostSendCoordinator()
    sampler = FakeBaselineSampler()
    listener = keyboard_listener.KeyboardListener(
        lambda event: None,
        post_send_coordinator=coordinator,
        baseline_sampler=sampler,
    )
    configure_listener_context(listener, status="ok", secure=secure)
    if not secure:
        monkeypatch.setattr(
            keyboard_listener.config, "input_capture_mode", "count-only"
        )
    monkeypatch.setattr(keyboard_listener, "get_app_by_pid", lambda pid: ("Kim", "Kem"))

    listener._process_raw_event(
        keyboard_listener.RawKeyboardEvent(
            event_type=keyboard_listener.kCGEventKeyDown,
            keycode=keyboard_listener.ENTER_KEYCODE,
            text="",
            app_name="Kim",
            bundle_id="Kem",
            modifiers={"shift": False, "ctrl": False, "alt": False, "cmd": False},
            target_pid=123,
        )
    )

    assert coordinator.submitted == []
    assert sampler.taken == []


@pytest.mark.parametrize(
    ("modifiers", "autorepeat"),
    (
        ({"shift": True, "ctrl": False, "alt": False, "cmd": False}, False),
        ({"shift": False, "ctrl": False, "alt": False, "cmd": False}, True),
    ),
)
def test_postsend_modifier_or_autorepeat_enter_does_not_create_intent(
    monkeypatch,
    modifiers,
    autorepeat,
):
    keyboard_listener, _ = import_keyboard_listener(monkeypatch)
    coordinator = FakePostSendCoordinator()
    listener = keyboard_listener.KeyboardListener(
        lambda event: None,
        post_send_coordinator=coordinator,
        baseline_sampler=FakeBaselineSampler(),
    )
    monkeypatch.setattr(keyboard_listener, "get_app_by_pid", lambda pid: ("Kim", "Kem"))

    listener._process_raw_event(
        keyboard_listener.RawKeyboardEvent(
            event_type=keyboard_listener.kCGEventKeyDown,
            keycode=keyboard_listener.ENTER_KEYCODE,
            text="",
            app_name="Kim",
            bundle_id="Kem",
            modifiers=modifiers,
            target_pid=123,
            is_autorepeat=autorepeat,
        )
    )

    assert coordinator.submitted == []


def test_postsend_queue_full_records_diagnostic_without_content(monkeypatch):
    keyboard_listener, _ = import_keyboard_listener(monkeypatch)
    diagnostics = []
    coordinator = FakePostSendCoordinator(accepts=False)
    listener = keyboard_listener.KeyboardListener(
        lambda event: None,
        diagnostics_callback=diagnostics.append,
        post_send_coordinator=coordinator,
        baseline_sampler=FakeBaselineSampler(),
    )
    configure_listener_context(listener, status="degraded", secure=False)
    monkeypatch.setattr(keyboard_listener, "get_app_by_pid", lambda pid: ("Kim", "Kem"))

    listener._process_raw_event(
        keyboard_listener.RawKeyboardEvent(
            event_type=keyboard_listener.kCGEventKeyDown,
            keycode=keyboard_listener.ENTER_KEYCODE,
            text="",
            app_name="Kim",
            bundle_id="Kem",
            modifiers={"shift": False, "ctrl": False, "alt": False, "cmd": False},
            target_pid=123,
        )
    )

    assert diagnostics[-1]["decision_reason"] == "post_send_queue_full"
    assert "content" not in repr(diagnostics[-1])


def test_postsend_success_uses_existing_submission_event_path(monkeypatch):
    keyboard_listener, _ = import_keyboard_listener(monkeypatch)
    events = []
    coordinator = FakePostSendCoordinator()
    listener = keyboard_listener.KeyboardListener(
        events.append,
        post_send_coordinator=coordinator,
        baseline_sampler=FakeBaselineSampler(),
    )
    configure_listener_context(listener, status="degraded", secure=False)
    monkeypatch.setattr(keyboard_listener, "get_app_by_pid", lambda pid: ("Kim", "Kem"))
    monkeypatch.setattr(
        keyboard_listener,
        "get_current_app_target",
        lambda: ("Kim", "Kem", 123),
    )
    listener._process_raw_event(
        keyboard_listener.RawKeyboardEvent(
            event_type=keyboard_listener.kCGEventKeyDown,
            keycode=keyboard_listener.ENTER_KEYCODE,
            text="",
            app_name="Kim",
            bundle_id="Kem",
            modifiers={"shift": False, "ctrl": False, "alt": False, "cmd": False},
            target_pid=123,
        )
    )
    intent = coordinator.submitted[0]

    listener._handle_post_send_success(
        keyboard_listener.CaptureOutcome(
            intent_id=intent.intent_id,
            content="最终文本",
            source="kim_postsend_ocr",
            message_identity="bubble-1",
            session_anchor="session-a",
            window_id=42,
        )
    )

    assert len(events) == 1
    assert events[0].character == "最终文本"
    assert events[0].modifiers["submit_snapshot"] is True
    assert events[0].modifiers["fallback_source"] == "kim_postsend_ocr"
    assert events[0].modifiers["context"] == {}


def test_postsend_failure_and_app_switch_emit_no_content(monkeypatch):
    keyboard_listener, _ = import_keyboard_listener(monkeypatch)
    events = []
    diagnostics = []
    coordinator = FakePostSendCoordinator()
    listener = keyboard_listener.KeyboardListener(
        events.append,
        diagnostics_callback=diagnostics.append,
        post_send_coordinator=coordinator,
        baseline_sampler=FakeBaselineSampler(),
    )
    configure_listener_context(listener, status="degraded", secure=False)
    monkeypatch.setattr(keyboard_listener, "get_app_by_pid", lambda pid: ("Kim", "Kem"))
    listener._process_raw_event(
        keyboard_listener.RawKeyboardEvent(
            event_type=keyboard_listener.kCGEventKeyDown,
            keycode=keyboard_listener.ENTER_KEYCODE,
            text="",
            app_name="Kim",
            bundle_id="Kem",
            modifiers={"shift": False, "ctrl": False, "alt": False, "cmd": False},
            target_pid=123,
        )
    )
    first = coordinator.submitted[0]
    listener._handle_post_send_failure(
        keyboard_listener.CaptureOutcome(
            intent_id=first.intent_id,
            failure_reason="capture_timeout",
        )
    )
    assert events == []
    assert diagnostics[-1]["decision_reason"] == "capture_timeout"

    listener._process_raw_event(
        keyboard_listener.RawKeyboardEvent(
            event_type=keyboard_listener.kCGEventKeyDown,
            keycode=keyboard_listener.ENTER_KEYCODE,
            text="",
            app_name="Kim",
            bundle_id="Kem",
            modifiers={"shift": False, "ctrl": False, "alt": False, "cmd": False},
            target_pid=123,
        )
    )
    second = coordinator.submitted[1]
    monkeypatch.setattr(
        keyboard_listener,
        "get_current_app_target",
        lambda: ("Notes", "com.apple.Notes", 999),
    )
    listener._handle_post_send_success(
        keyboard_listener.CaptureOutcome(
            intent_id=second.intent_id,
            content="不能保存",
            source="kim_postsend_ocr",
            message_identity="bubble-2",
            session_anchor="session-a",
            window_id=42,
        )
    )
    assert events == []
    assert diagnostics[-1]["decision_reason"] == "post_send_target_changed"


def test_postsend_success_fails_closed_when_current_target_is_unknown(
    monkeypatch,
):
    keyboard_listener, _ = import_keyboard_listener(monkeypatch)
    events = []
    diagnostics = []
    coordinator = FakePostSendCoordinator()
    listener = keyboard_listener.KeyboardListener(
        events.append,
        diagnostics_callback=diagnostics.append,
        post_send_coordinator=coordinator,
        baseline_sampler=FakeBaselineSampler(),
    )
    configure_listener_context(listener, status="degraded", secure=False)
    monkeypatch.setattr(keyboard_listener, "get_app_by_pid", lambda pid: ("Kim", "Kem"))
    listener._process_raw_event(
        keyboard_listener.RawKeyboardEvent(
            event_type=keyboard_listener.kCGEventKeyDown,
            keycode=keyboard_listener.ENTER_KEYCODE,
            text="",
            app_name="Kim",
            bundle_id="Kem",
            modifiers={"shift": False, "ctrl": False, "alt": False, "cmd": False},
            target_pid=123,
        )
    )
    intent = coordinator.submitted[0]
    monkeypatch.setattr(
        keyboard_listener,
        "get_current_app_target",
        lambda: ("Unknown", "unknown", -1),
    )

    listener._handle_post_send_success(
        keyboard_listener.CaptureOutcome(
            intent_id=intent.intent_id,
            content="不能保存",
            source="kim_postsend_ocr",
            message_identity="bubble-unknown",
            session_anchor="session-a",
            window_id=42,
        )
    )

    assert events == []
    assert diagnostics[-1]["decision_reason"] == "post_send_target_changed"


def test_postsend_success_rejects_same_window_conversation_switch(monkeypatch):
    keyboard_listener, _ = import_keyboard_listener(monkeypatch)
    events = []
    diagnostics = []
    coordinator = FakePostSendCoordinator()
    listener = keyboard_listener.KeyboardListener(
        events.append,
        diagnostics_callback=diagnostics.append,
        post_send_coordinator=coordinator,
        baseline_sampler=FakeBaselineSampler(),
    )
    configure_listener_context(listener, status="degraded", secure=False)
    monkeypatch.setattr(keyboard_listener, "get_app_by_pid", lambda pid: ("Kim", "Kem"))
    monkeypatch.setattr(
        keyboard_listener,
        "get_current_app_target",
        lambda: ("Kim", "Kem", 123),
    )
    listener._process_raw_event(
        keyboard_listener.RawKeyboardEvent(
            event_type=keyboard_listener.kCGEventKeyDown,
            keycode=keyboard_listener.ENTER_KEYCODE,
            text="",
            app_name="Kim",
            bundle_id="Kem",
            modifiers={"shift": False, "ctrl": False, "alt": False, "cmd": False},
            target_pid=123,
        )
    )
    intent = coordinator.submitted[0]

    listener._handle_post_send_success(
        keyboard_listener.CaptureOutcome(
            intent_id=intent.intent_id,
            content="另一会话",
            source="kim_postsend_ocr",
            message_identity="bubble-other-chat",
            session_anchor="session-b",
            window_id=42,
        )
    )

    assert events == []
    assert diagnostics[-1]["decision_reason"] == "post_send_session_changed"


def test_postsend_cmd_v_schedules_baseline_on_worker(monkeypatch):
    keyboard_listener, _ = import_keyboard_listener(monkeypatch)
    sampler = FakeBaselineSampler()
    listener = keyboard_listener.KeyboardListener(
        lambda event: None,
        post_send_coordinator=FakePostSendCoordinator(),
        baseline_sampler=sampler,
    )
    configure_listener_context(listener, status="degraded", secure=False)
    monkeypatch.setattr(keyboard_listener, "get_app_by_pid", lambda pid: ("Kim", "Kem"))

    listener._process_raw_event(
        keyboard_listener.RawKeyboardEvent(
            event_type=keyboard_listener.kCGEventKeyDown,
            keycode=9,
            text="",
            app_name="Kim",
            bundle_id="Kem",
            modifiers={"shift": False, "ctrl": False, "alt": False, "cmd": True},
            target_pid=123,
        )
    )

    assert sampler.scheduled == [123]


def test_postsend_cmd_v_keyup_refreshes_one_validation_snapshot(monkeypatch):
    keyboard_listener, _ = import_keyboard_listener(monkeypatch)
    snapshots = []
    listener = keyboard_listener.KeyboardListener(
        lambda event: None,
        post_send_coordinator=FakePostSendCoordinator(),
        baseline_sampler=FakeBaselineSampler(),
    )
    listener._record_recent_text_snapshot = lambda app_name, bundle_id, **kwargs: (
        snapshots.append((app_name, bundle_id))
    )
    monkeypatch.setattr(keyboard_listener, "get_app_by_pid", lambda pid: ("Kim", "Kem"))

    listener._process_raw_event(
        keyboard_listener.RawKeyboardEvent(
            event_type=keyboard_listener.kCGEventKeyUp,
            keycode=9,
            text="",
            app_name="Kim",
            bundle_id="Kem",
            modifiers={"shift": False, "ctrl": False, "alt": False, "cmd": True},
            target_pid=123,
        )
    )

    assert snapshots == [("Kim", "Kem")]


def test_postsend_paste_only_enter_submits_with_recent_baseline(monkeypatch):
    keyboard_listener, _ = import_keyboard_listener(monkeypatch)
    sampler = FakeBaselineSampler()
    coordinator = FakePostSendCoordinator()
    listener = keyboard_listener.KeyboardListener(
        lambda event: None,
        post_send_coordinator=coordinator,
        baseline_sampler=sampler,
    )
    configure_listener_context(listener, status="degraded", secure=False)
    listener._read_post_send_paste_text = lambda: "paste-only-2356"
    listener._get_post_send_paste_change_count = lambda: 41
    monkeypatch.setattr(keyboard_listener, "get_app_by_pid", lambda pid: ("Kim", "Kem"))
    common = dict(
        app_name="Kim",
        bundle_id="Kem",
        target_pid=123,
        text="",
    )

    listener._process_raw_event(
        keyboard_listener.RawKeyboardEvent(
            event_type=keyboard_listener.kCGEventKeyDown,
            keycode=9,
            modifiers={"shift": False, "ctrl": False, "alt": False, "cmd": True},
            **common,
        )
    )
    listener._process_raw_event(
        keyboard_listener.RawKeyboardEvent(
            event_type=keyboard_listener.kCGEventKeyUp,
            keycode=9,
            modifiers={"shift": False, "ctrl": False, "alt": False, "cmd": True},
            **common,
        )
    )
    listener._process_raw_event(
        keyboard_listener.RawKeyboardEvent(
            event_type=keyboard_listener.kCGEventKeyDown,
            keycode=keyboard_listener.ENTER_KEYCODE,
            modifiers={"shift": False, "ctrl": False, "alt": False, "cmd": False},
            **common,
        )
    )

    assert sampler.scheduled == [123]
    assert sampler.taken == [123]
    assert len(coordinator.submitted) == 1
    assert coordinator.submitted[0].baseline.session_anchor == "session-a"
    assert coordinator.submitted[0].validation_text == "paste-only-2356"
    assert sampler.take_waits == [keyboard_listener.POST_SEND_BASELINE_WAIT_SECONDS]


def test_secure_postsend_paste_never_reads_clipboard_text(monkeypatch):
    keyboard_listener, _ = import_keyboard_listener(monkeypatch)
    coordinator = FakePostSendCoordinator()
    listener = keyboard_listener.KeyboardListener(
        lambda event: None,
        post_send_coordinator=coordinator,
        baseline_sampler=FakeBaselineSampler(),
    )
    configure_listener_context(listener, status="ok", secure=True)
    listener._get_post_send_paste_change_count = lambda: 42
    listener._read_post_send_paste_text = lambda: (_ for _ in ()).throw(
        AssertionError("secure paste must not read clipboard text")
    )
    monkeypatch.setattr(keyboard_listener, "get_app_by_pid", lambda pid: ("Kim", "Kem"))

    for event_type, keycode, modifiers in (
        (
            keyboard_listener.kCGEventKeyDown,
            9,
            {"shift": False, "ctrl": False, "alt": False, "cmd": True},
        ),
        (
            keyboard_listener.kCGEventKeyUp,
            9,
            {"shift": False, "ctrl": False, "alt": False, "cmd": True},
        ),
        (
            keyboard_listener.kCGEventKeyDown,
            keyboard_listener.ENTER_KEYCODE,
            {"shift": False, "ctrl": False, "alt": False, "cmd": False},
        ),
    ):
        listener._process_raw_event(
            keyboard_listener.RawKeyboardEvent(
                event_type=event_type,
                keycode=keycode,
                text="",
                app_name="Kim",
                bundle_id="Kem",
                modifiers=modifiers,
                target_pid=123,
            )
        )

    assert coordinator.submitted == []
    assert listener._recent_paste_validations == {}


def test_mixed_postsend_input_never_uses_paste_fragment_as_full_validation(
    monkeypatch,
):
    keyboard_listener, _ = import_keyboard_listener(monkeypatch)
    coordinator = FakePostSendCoordinator()
    listener = keyboard_listener.KeyboardListener(
        lambda event: None,
        post_send_coordinator=coordinator,
        baseline_sampler=FakeBaselineSampler(),
    )
    configure_listener_context(listener, status="degraded", secure=False)
    listener._get_post_send_paste_change_count = lambda: 43
    listener._read_post_send_paste_text = lambda: "paste-fragment"
    listener._record_recent_text_snapshot = lambda app_name, bundle_id, **kwargs: (
        listener._recent_text_snapshots.__setitem__(
            (app_name, bundle_id),
            ("paste-fragment", time.monotonic(), None),
        )
    )
    monkeypatch.setattr(keyboard_listener, "get_app_by_pid", lambda pid: ("Kim", "Kem"))
    common = dict(app_name="Kim", bundle_id="Kem", target_pid=123)

    listener._process_raw_event(
        keyboard_listener.RawKeyboardEvent(
            event_type=keyboard_listener.kCGEventKeyDown,
            keycode=9,
            text="",
            modifiers={"shift": False, "ctrl": False, "alt": False, "cmd": True},
            **common,
        )
    )
    listener._process_raw_event(
        keyboard_listener.RawKeyboardEvent(
            event_type=keyboard_listener.kCGEventKeyUp,
            keycode=9,
            text="",
            modifiers={"shift": False, "ctrl": False, "alt": False, "cmd": True},
            **common,
        )
    )
    listener._process_raw_event(
        keyboard_listener.RawKeyboardEvent(
            event_type=keyboard_listener.kCGEventKeyDown,
            keycode=0,
            text="x",
            modifiers={"shift": False, "ctrl": False, "alt": False, "cmd": False},
            **common,
        )
    )
    listener._process_raw_event(
        keyboard_listener.RawKeyboardEvent(
            event_type=keyboard_listener.kCGEventKeyDown,
            keycode=keyboard_listener.ENTER_KEYCODE,
            text="",
            modifiers={"shift": False, "ctrl": False, "alt": False, "cmd": False},
            **common,
        )
    )

    assert len(coordinator.submitted) == 1
    assert coordinator.submitted[0].validation_text == ""


def test_postsend_enter_uses_multiline_recent_snapshot_for_validation(monkeypatch):
    keyboard_listener, _ = import_keyboard_listener(monkeypatch)
    coordinator = FakePostSendCoordinator()
    listener = keyboard_listener.KeyboardListener(
        lambda event: None,
        post_send_coordinator=coordinator,
        baseline_sampler=FakeBaselineSampler(),
    )
    configure_listener_context(listener, status="degraded", secure=False)
    listener._recent_text_snapshots[("Kim", "Kem")] = (
        "第一行\n第二行",
        time.monotonic(),
        None,
    )
    monkeypatch.setattr(keyboard_listener, "get_app_by_pid", lambda pid: ("Kim", "Kem"))

    listener._process_raw_event(
        keyboard_listener.RawKeyboardEvent(
            event_type=keyboard_listener.kCGEventKeyDown,
            keycode=keyboard_listener.ENTER_KEYCODE,
            text="",
            app_name="Kim",
            bundle_id="Kem",
            modifiers={"shift": False, "ctrl": False, "alt": False, "cmd": False},
            target_pid=123,
        )
    )

    assert coordinator.submitted[0].validation_text == "第一行\n第二行"


def test_postsend_typed_latin_uses_local_key_buffer_for_validation(monkeypatch):
    keyboard_listener, _ = import_keyboard_listener(monkeypatch)
    coordinator = FakePostSendCoordinator()
    listener = keyboard_listener.KeyboardListener(
        lambda event: None,
        post_send_coordinator=coordinator,
        baseline_sampler=FakeBaselineSampler(),
    )
    configure_listener_context(listener, status="degraded", secure=False)
    monkeypatch.setattr(keyboard_listener, "get_app_by_pid", lambda pid: ("Kim", "Kem"))
    common = dict(
        event_type=keyboard_listener.kCGEventKeyDown,
        app_name="Kim",
        bundle_id="Kem",
        modifiers={"shift": False, "ctrl": False, "alt": False, "cmd": False},
        target_pid=123,
    )

    listener._process_raw_event(
        keyboard_listener.RawKeyboardEvent(keycode=0, text="a", **common)
    )
    listener._process_raw_event(
        keyboard_listener.RawKeyboardEvent(keycode=11, text="b", **common)
    )
    listener._process_raw_event(
        keyboard_listener.RawKeyboardEvent(
            keycode=keyboard_listener.ENTER_KEYCODE,
            text="",
            **common,
        )
    )

    assert coordinator.submitted[0].validation_text == "ab"


def test_postsend_shift_enter_refreshes_multiline_validation_snapshot(monkeypatch):
    keyboard_listener, _ = import_keyboard_listener(monkeypatch)
    snapshots = []
    listener = keyboard_listener.KeyboardListener(
        lambda event: None,
        post_send_coordinator=FakePostSendCoordinator(),
        baseline_sampler=FakeBaselineSampler(),
    )
    listener._record_recent_text_snapshot = lambda app_name, bundle_id, **kwargs: (
        snapshots.append((app_name, bundle_id))
    )
    monkeypatch.setattr(keyboard_listener, "get_app_by_pid", lambda pid: ("Kim", "Kem"))

    listener._process_raw_event(
        keyboard_listener.RawKeyboardEvent(
            event_type=keyboard_listener.kCGEventKeyDown,
            keycode=keyboard_listener.ENTER_KEYCODE,
            text="",
            app_name="Kim",
            bundle_id="Kem",
            modifiers={"shift": True, "ctrl": False, "alt": False, "cmd": False},
            target_pid=123,
        )
    )

    assert snapshots == [("Kim", "Kem")]


def test_postsend_shift_enter_is_preserved_in_key_buffer_validation(monkeypatch):
    keyboard_listener, _ = import_keyboard_listener(monkeypatch)
    coordinator = FakePostSendCoordinator()
    listener = keyboard_listener.KeyboardListener(
        lambda event: None,
        post_send_coordinator=coordinator,
        baseline_sampler=FakeBaselineSampler(),
    )
    configure_listener_context(listener, status="degraded", secure=False)
    monkeypatch.setattr(keyboard_listener, "get_app_by_pid", lambda pid: ("Kim", "Kem"))

    def event(keycode, text="", *, shift=False):
        return keyboard_listener.RawKeyboardEvent(
            event_type=keyboard_listener.kCGEventKeyDown,
            keycode=keycode,
            text=text,
            app_name="Kim",
            bundle_id="Kem",
            modifiers={"shift": shift, "ctrl": False, "alt": False, "cmd": False},
            target_pid=123,
        )

    listener._process_raw_event(event(0, "a"))
    listener._process_raw_event(event(keyboard_listener.ENTER_KEYCODE, shift=True))
    listener._process_raw_event(event(11, "b"))
    listener._process_raw_event(event(keyboard_listener.ENTER_KEYCODE))

    assert coordinator.submitted[0].validation_text == "a\nb"


def test_unsupported_app_uses_native_target(monkeypatch):
    keyboard_listener, _ = import_keyboard_listener(monkeypatch)
    listener = keyboard_listener.KeyboardListener(lambda event: None)
    listener._record_recent_text_snapshot = lambda *args, **kwargs: None
    monkeypatch.setattr(
        keyboard_listener,
        "get_app_by_pid",
        lambda pid: ("Notes", "com.apple.Notes"),
    )

    listener._process_raw_event(
        keyboard_listener.RawKeyboardEvent(
            event_type=keyboard_listener.kCGEventKeyDown,
            keycode=8,
            text="c",
            app_name="Chrome",
            bundle_id="com.google.Chrome",
            modifiers={"shift": False, "ctrl": False, "alt": False, "cmd": False},
            target_pid=4321,
            frontmost_pid=9876,
        )
    )

    assert listener._fallback_buffers[("Notes", "com.apple.Notes")] == ["c"]
    assert ("Chrome", "com.google.Chrome") not in listener._fallback_buffers


def test_event_tap_callback_never_processes_synchronously_after_start(monkeypatch):
    keyboard_listener, _ = import_keyboard_listener(monkeypatch)
    listener = keyboard_listener.KeyboardListener(lambda event: None)
    listener._has_started = True
    listener._event_worker_running = False
    listener._capture_focused_context = lambda **kwargs: (_ for _ in ()).throw(
        AssertionError("native callback must never read Accessibility")
    )
    monkeypatch.setattr(
        keyboard_listener,
        "get_current_app_target",
        lambda: ("Codex", "com.openai.codex", 0),
    )

    listener._event_callback(
        None,
        keyboard_listener.kCGEventKeyDown,
        SimpleNamespace(keycode=keyboard_listener.ENTER_KEYCODE, text=""),
        None,
    )

    assert listener._dropped_event_count == 1


def test_submission_context_capture_receives_event_target_pid(monkeypatch):
    keyboard_listener, _ = import_keyboard_listener(monkeypatch)
    events = []
    capture_calls = []
    listener = keyboard_listener.KeyboardListener(events.append)
    monkeypatch.setattr(
        keyboard_listener,
        "get_app_by_pid",
        lambda pid: ("Codex", "com.openai.codex"),
    )

    def fake_capture(*, target_pid=None, **_kwargs):
        capture_calls.append(target_pid)
        return SimpleNamespace(
            focused_role="AXTextArea",
            focused_subrole=None,
            focused_protected=False,
            focused_value="targeted context",
            capture_status="ok",
        )

    listener._capture_focused_context = fake_capture
    listener._context_to_dict_safe = lambda context: {"capture_status": "ok"}

    listener._process_raw_event(
        keyboard_listener.RawKeyboardEvent(
            event_type=keyboard_listener.kCGEventKeyDown,
            keycode=keyboard_listener.ENTER_KEYCODE,
            text="",
            app_name="Codex",
            bundle_id="com.openai.codex",
            modifiers={"shift": False, "ctrl": False, "alt": False, "cmd": False},
            target_pid=123,
        )
    )

    assert capture_calls == [123]
    assert len(events) == 1


def test_start_refuses_to_spawn_second_live_event_worker(monkeypatch):
    keyboard_listener, _ = import_keyboard_listener(monkeypatch)
    listener = keyboard_listener.KeyboardListener(lambda event: None)
    listener._event_worker_thread = SimpleNamespace(is_alive=lambda: True)

    try:
        listener.start()
    except RuntimeError as exc:
        assert "still stopping" in str(exc)
    else:
        raise AssertionError("start must not create a second event worker")


def test_start_stop_start_recreates_owned_postsend_services(monkeypatch):
    keyboard_listener, _ = import_keyboard_listener(monkeypatch)
    samplers = []
    coordinators = []

    class OwnedSampler:
        def __init__(self):
            self.stopped = False
            samplers.append(self)

        def capture_current_frame(self, pid):
            return None

        def stop(self):
            self.stopped = True

    class OwnedCoordinator:
        def __init__(self, *args, **kwargs):
            self.stopped = False
            coordinators.append(self)

        def stop(self):
            self.stopped = True

    monkeypatch.setattr(keyboard_listener, "ChatWindowBaselineSampler", OwnedSampler)
    monkeypatch.setattr(
        keyboard_listener, "PostSendCaptureCoordinator", OwnedCoordinator
    )
    monkeypatch.setattr(
        keyboard_listener,
        "build_default_message_source_chain",
        lambda **kwargs: object(),
    )
    monkeypatch.setattr(keyboard_listener, "_start_app_watcher", lambda callback: None)
    monkeypatch.setattr(
        keyboard_listener, "_set_app_activation_callback", lambda callback: None
    )
    monkeypatch.setattr(keyboard_listener, "set_recording_status", lambda status: None)
    listener = keyboard_listener.KeyboardListener(lambda event: None)
    listener._start_wake_observer = lambda: None
    listener._run_loop_thread = lambda: None
    listener._health_check_loop = lambda: None

    listener.start()
    first_sampler = listener._baseline_sampler
    first_coordinator = listener._post_send_coordinator
    listener._recent_paste_validations[("Kim", "Kem")] = (
        "private-paste",
        time.monotonic(),
    )
    listener.stop()
    assert listener._recent_paste_validations == {}
    listener.start()
    second_sampler = listener._baseline_sampler
    second_coordinator = listener._post_send_coordinator
    listener.stop()

    assert first_sampler is not second_sampler
    assert first_coordinator is not second_coordinator
    assert first_sampler.stopped and second_sampler.stopped
    assert first_coordinator.stopped and second_coordinator.stopped


def test_app_watcher_start_waits_for_initial_composer_prepare(monkeypatch):
    keyboard_listener, _ = import_keyboard_listener(monkeypatch)
    prepare_started = threading.Event()
    allow_prepare = threading.Event()
    start_returned = threading.Event()
    keep_watcher_alive = threading.Event()

    class FakeApplication:
        def localizedName(self):
            return "微信"

        def bundleIdentifier(self):
            return "com.tencent.xinWeChat"

        def processIdentifier(self):
            return 4318

    class FakeNotificationCenter:
        def addObserver_selector_name_object_(self, *args):
            return None

    class FakeWorkspace:
        frontmost_calls = 0

        def notificationCenter(self):
            return FakeNotificationCenter()

        def frontmostApplication(self):
            self.frontmost_calls += 1
            if self.frontmost_calls == 1:
                return None
            return FakeApplication()

    class FakeRunLoop:
        def runMode_beforeDate_(self, mode, deadline):
            keep_watcher_alive.wait()

    class FakeNSObject:
        @classmethod
        def alloc(cls):
            return cls()

        def init(self):
            return self

    foundation = sys.modules["Foundation"]
    foundation.NSDate = SimpleNamespace(
        dateWithTimeIntervalSinceNow_=lambda seconds: seconds
    )
    monkeypatch.setattr(
        keyboard_listener.NSWorkspace,
        "sharedWorkspace",
        lambda: FakeWorkspace(),
    )
    monkeypatch.setattr(
        keyboard_listener,
        "NSRunLoop",
        SimpleNamespace(currentRunLoop=lambda: FakeRunLoop()),
    )
    monkeypatch.setattr(keyboard_listener, "NSObject", FakeNSObject)
    monkeypatch.setattr(keyboard_listener.objc, "super", super, raising=False)

    def prepare(app_name, bundle_id, target_pid):
        prepare_started.set()
        allow_prepare.wait(timeout=2)

    def start_watcher():
        keyboard_listener._start_app_watcher(prepare)
        start_returned.set()

    starter = threading.Thread(target=start_watcher, daemon=True)
    starter.start()

    assert prepare_started.wait(timeout=1)
    time.sleep(0.25)
    assert not start_returned.is_set()

    allow_prepare.set()
    assert start_returned.wait(timeout=1)


def test_start_does_not_leave_worker_running_when_app_watcher_fails(
    monkeypatch,
):
    keyboard_listener, _ = import_keyboard_listener(monkeypatch)
    listener = keyboard_listener.KeyboardListener(lambda event: None)
    monkeypatch.setattr(
        keyboard_listener,
        "_start_app_watcher",
        lambda callback: (_ for _ in ()).throw(
            RuntimeError("app watcher initialization failed")
        ),
    )

    with pytest.raises(RuntimeError, match="app watcher initialization failed"):
        listener.start()

    assert not listener._running
    assert not listener._has_started
    assert not listener._event_worker_running
    assert listener._event_worker_thread is None


def test_event_worker_failure_is_reported_to_runtime_state(monkeypatch):
    keyboard_listener, _ = import_keyboard_listener(monkeypatch)
    errors = []
    listener = keyboard_listener.KeyboardListener(lambda event: None)
    listener._event_worker_running = False
    listener._event_queue.put(
        keyboard_listener.RawKeyboardEvent(
            event_type=keyboard_listener.kCGEventKeyDown,
            keycode=8,
            text="c",
            app_name="Codex",
            bundle_id="com.openai.codex",
            modifiers={},
        )
    )
    monkeypatch.setattr(
        listener,
        "_process_raw_event",
        lambda _event: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    monkeypatch.setattr(
        keyboard_listener,
        "record_runtime_error",
        errors.append,
        raising=False,
    )

    listener._event_worker_loop()

    assert errors == ["event_worker_failed:boom"]


def test_enter_keydown_and_keyup_produce_one_submission_attempt(monkeypatch):
    keyboard_listener, _ = import_keyboard_listener(monkeypatch)
    events = []
    diagnostics = []
    listener = keyboard_listener.KeyboardListener(
        events.append, diagnostics_callback=diagnostics.append
    )
    listener._get_event_target_app = lambda event: ("Codex", "com.openai.codex")
    listener._get_focused_text_snapshot = lambda: "one submission"
    monkeypatch.setattr(
        keyboard_listener,
        "capture_accessibility_context",
        lambda: SimpleNamespace(
            focused_role="AXTextArea",
            focused_subrole=None,
            focused_protected=False,
            capture_status="ok",
        ),
    )
    monkeypatch.setattr(keyboard_listener, "context_to_dict", lambda context: {})

    enter = SimpleNamespace(keycode=keyboard_listener.ENTER_KEYCODE, text="")
    listener._event_callback(None, keyboard_listener.kCGEventKeyDown, enter, None)
    listener._event_callback(None, keyboard_listener.kCGEventKeyUp, enter, None)

    assert len(events) == 1
    assert diagnostics == []


def test_enter_keyup_still_finishes_same_attempt_after_long_key_hold(monkeypatch):
    keyboard_listener, _ = import_keyboard_listener(monkeypatch)
    listener = keyboard_listener.KeyboardListener(lambda event: None)
    now = [100.0]
    monkeypatch.setattr(keyboard_listener.time, "monotonic", lambda: now[0])

    listener._ignore_enter_keyup_once("Codex", "com.openai.codex")
    now[0] += 5.0

    assert listener._should_ignore_enter_keyup(
        "Codex",
        "com.openai.codex",
        keyboard_listener.kCGEventKeyUp,
    )


def test_enter_autorepeat_does_not_create_another_attempt(monkeypatch):
    keyboard_listener, _ = import_keyboard_listener(monkeypatch)
    events = []
    listener = keyboard_listener.KeyboardListener(events.append)
    listener._get_event_target_app = lambda event: ("Codex", "com.openai.codex")
    listener._get_focused_text_snapshot = lambda: "submit once"
    monkeypatch.setattr(
        keyboard_listener,
        "capture_accessibility_context",
        lambda: SimpleNamespace(
            focused_role="AXTextArea",
            focused_subrole=None,
            focused_protected=False,
            capture_status="ok",
        ),
    )
    monkeypatch.setattr(keyboard_listener, "context_to_dict", lambda context: {})

    listener._event_callback(
        None,
        keyboard_listener.kCGEventKeyDown,
        SimpleNamespace(keycode=keyboard_listener.ENTER_KEYCODE, autorepeat=0, text=""),
        None,
    )
    listener._event_callback(
        None,
        keyboard_listener.kCGEventKeyDown,
        SimpleNamespace(keycode=keyboard_listener.ENTER_KEYCODE, autorepeat=1, text=""),
        None,
    )

    assert len(events) == 1


def test_new_enter_keydown_recovers_when_previous_keyup_was_lost(monkeypatch):
    keyboard_listener, _ = import_keyboard_listener(monkeypatch)
    events = []
    listener = keyboard_listener.KeyboardListener(events.append)
    listener._get_event_target_app = lambda event: ("Codex", "com.openai.codex")
    listener._get_focused_text_snapshot = lambda: "submit again"
    monkeypatch.setattr(
        keyboard_listener,
        "capture_accessibility_context",
        lambda: SimpleNamespace(
            focused_role="AXTextArea",
            focused_subrole=None,
            focused_protected=False,
            capture_status="ok",
        ),
    )
    monkeypatch.setattr(keyboard_listener, "context_to_dict", lambda context: {})

    enter = SimpleNamespace(
        keycode=keyboard_listener.ENTER_KEYCODE,
        autorepeat=0,
        text="",
    )
    listener._event_callback(None, keyboard_listener.kCGEventKeyDown, enter, None)
    listener._event_callback(None, keyboard_listener.kCGEventKeyDown, enter, None)

    assert len(events) == 2


def test_shift_enter_is_newline_not_submission(monkeypatch):
    keyboard_listener, _ = import_keyboard_listener(monkeypatch)
    events = []
    diagnostics = []
    listener = keyboard_listener.KeyboardListener(
        events.append, diagnostics_callback=diagnostics.append
    )
    listener._get_event_target_app = lambda event: ("Codex", "com.openai.codex")
    listener._get_focused_text_snapshot = lambda: "draft"
    monkeypatch.setattr(
        keyboard_listener,
        "capture_accessibility_context",
        lambda: (_ for _ in ()).throw(AssertionError("newline must not read AX")),
    )

    listener._event_callback(
        None,
        keyboard_listener.kCGEventKeyDown,
        SimpleNamespace(
            keycode=keyboard_listener.ENTER_KEYCODE,
            text="",
            flags=keyboard_listener.kCGEventFlagMaskShift,
        ),
        None,
    )

    assert events == []
    assert diagnostics[-1]["decision_reason"] == "newline_modifier"


def test_secure_field_enter_never_saves_text(monkeypatch):
    keyboard_listener, _ = import_keyboard_listener(monkeypatch)
    events = []
    diagnostics = []
    listener = keyboard_listener.KeyboardListener(
        events.append, diagnostics_callback=diagnostics.append
    )
    listener._get_event_target_app = lambda event: ("Safari", "com.apple.Safari")
    listener._get_focused_text_snapshot = lambda: "super secret"
    monkeypatch.setattr(
        keyboard_listener,
        "capture_accessibility_context",
        lambda: SimpleNamespace(
            focused_role="AXTextField",
            focused_subrole="AXSecureTextField",
            focused_protected=True,
            capture_status="ok",
        ),
    )
    monkeypatch.setattr(
        keyboard_listener,
        "context_to_dict",
        lambda context: {
            "focused_role": context.focused_role,
            "focused_subrole": context.focused_subrole,
            "focused_protected": context.focused_protected,
            "capture_status": context.capture_status,
        },
    )

    listener._event_callback(
        None,
        keyboard_listener.kCGEventKeyDown,
        SimpleNamespace(keycode=keyboard_listener.ENTER_KEYCODE, text=""),
        None,
    )

    assert events == []
    assert diagnostics[-1]["decision_reason"] == "secure_text_input"


def test_oversized_ax_value_is_rejected_as_probable_document(monkeypatch):
    keyboard_listener, _ = import_keyboard_listener(monkeypatch)
    events = []
    diagnostics = []
    listener = keyboard_listener.KeyboardListener(
        events.append, diagnostics_callback=diagnostics.append
    )
    listener._get_event_target_app = lambda event: (
        "Claude",
        "com.anthropic.claudefordesktop",
    )
    listener._get_focused_text_snapshot = lambda: (
        "x" * (keyboard_listener.MAX_TRUSTED_SUBMISSION_CHARS + 1)
    )
    monkeypatch.setattr(
        keyboard_listener,
        "capture_accessibility_context",
        lambda: SimpleNamespace(
            focused_role="AXTextArea",
            focused_subrole=None,
            focused_protected=False,
            capture_status="ok",
        ),
    )
    monkeypatch.setattr(keyboard_listener, "context_to_dict", lambda context: {})

    listener._event_callback(
        None,
        keyboard_listener.kCGEventKeyDown,
        SimpleNamespace(keycode=keyboard_listener.ENTER_KEYCODE, text=""),
        None,
    )

    assert events == []
    assert diagnostics[-1]["decision_reason"] == "suspected_whole_document"


def test_enter_in_editor_text_area_is_newline_not_submission(monkeypatch):
    keyboard_listener, _ = import_keyboard_listener(monkeypatch)
    events = []
    diagnostics = []
    listener = keyboard_listener.KeyboardListener(
        events.append, diagnostics_callback=diagnostics.append
    )
    listener._get_event_target_app = lambda event: ("TextEdit", "com.apple.TextEdit")
    listener._get_focused_text_snapshot = lambda: "a short document"
    monkeypatch.setattr(
        keyboard_listener,
        "capture_accessibility_context",
        lambda: SimpleNamespace(
            focused_role="AXTextArea",
            focused_subrole=None,
            focused_protected=False,
            capture_status="ok",
        ),
    )
    monkeypatch.setattr(keyboard_listener, "context_to_dict", lambda context: {})

    listener._event_callback(
        None,
        keyboard_listener.kCGEventKeyDown,
        SimpleNamespace(keycode=keyboard_listener.ENTER_KEYCODE, text=""),
        None,
    )

    assert events == []
    assert diagnostics[-1]["decision_reason"] == "editor_newline"


def test_enter_in_generic_browser_text_area_is_newline_not_submission(monkeypatch):
    keyboard_listener, _ = import_keyboard_listener(monkeypatch)
    events = []
    diagnostics = []
    listener = keyboard_listener.KeyboardListener(
        events.append, diagnostics_callback=diagnostics.append
    )
    listener._get_event_target_app = lambda event: ("Safari", "com.apple.Safari")
    listener._get_focused_text_snapshot = lambda: "short document body"
    monkeypatch.setattr(
        keyboard_listener,
        "capture_accessibility_context",
        lambda: SimpleNamespace(
            focused_role="AXTextArea",
            focused_subrole=None,
            focused_identifier="article-body",
            focused_title=None,
            focused_description=None,
            focused_protected=False,
            capture_status="ok",
        ),
    )
    monkeypatch.setattr(keyboard_listener, "context_to_dict", lambda context: {})

    listener._event_callback(
        None,
        keyboard_listener.kCGEventKeyDown,
        SimpleNamespace(keycode=keyboard_listener.ENTER_KEYCODE, text=""),
        None,
    )

    assert events == []
    assert diagnostics[-1]["decision_reason"] == "editor_newline"


def test_enter_in_browser_prompt_text_area_is_allowed(monkeypatch):
    keyboard_listener, _ = import_keyboard_listener(monkeypatch)
    events = []
    listener = keyboard_listener.KeyboardListener(events.append)
    listener._get_event_target_app = lambda event: ("Chrome", "com.google.Chrome")
    listener._get_focused_text_snapshot = lambda: "hello from browser chat"
    monkeypatch.setattr(
        keyboard_listener,
        "capture_accessibility_context",
        lambda: SimpleNamespace(
            focused_role="AXTextArea",
            focused_subrole=None,
            focused_identifier="prompt-textarea",
            focused_title=None,
            focused_description="Message composer",
            focused_protected=False,
            capture_status="ok",
        ),
    )
    monkeypatch.setattr(keyboard_listener, "context_to_dict", lambda context: {})

    listener._event_callback(
        None,
        keyboard_listener.kCGEventKeyDown,
        SimpleNamespace(keycode=keyboard_listener.ENTER_KEYCODE, text=""),
        None,
    )

    assert len(events) == 1


def test_enter_in_browser_message_composer_defaults_to_newline(monkeypatch):
    keyboard_listener, _ = import_keyboard_listener(monkeypatch)
    events = []
    diagnostics = []
    listener = keyboard_listener.KeyboardListener(
        events.append, diagnostics_callback=diagnostics.append
    )
    listener._get_event_target_app = lambda event: ("Chrome", "com.google.Chrome")
    listener._get_focused_text_snapshot = lambda: "email paragraph"
    monkeypatch.setattr(
        keyboard_listener,
        "capture_accessibility_context",
        lambda: SimpleNamespace(
            focused_role="AXTextArea",
            focused_subrole=None,
            focused_identifier="message-composer",
            focused_title="Message body",
            focused_description="Compose message",
            focused_protected=False,
            capture_status="ok",
        ),
    )
    monkeypatch.setattr(keyboard_listener, "context_to_dict", lambda context: {})

    listener._event_callback(
        None,
        keyboard_listener.kCGEventKeyDown,
        SimpleNamespace(keycode=keyboard_listener.ENTER_KEYCODE, text=""),
        None,
    )

    assert events == []
    assert diagnostics[-1]["decision_reason"] == "editor_newline"


def test_degraded_context_is_not_reported_as_non_text_focus(monkeypatch):
    keyboard_listener, _ = import_keyboard_listener(monkeypatch)
    events = []
    diagnostics = []
    listener = keyboard_listener.KeyboardListener(
        events.append,
        diagnostics_callback=diagnostics.append,
    )
    listener._get_event_target_app = lambda event: ("Codex", "com.openai.codex")
    monkeypatch.setattr(
        keyboard_listener,
        "capture_accessibility_context",
        lambda: SimpleNamespace(
            focused_role=None,
            focused_subrole=None,
            focused_protected=False,
            capture_status="degraded",
        ),
    )
    monkeypatch.setattr(
        keyboard_listener,
        "context_to_dict",
        lambda context: {"capture_status": "degraded"},
    )

    listener._event_callback(
        None,
        keyboard_listener.kCGEventKeyDown,
        SimpleNamespace(keycode=keyboard_listener.ENTER_KEYCODE, text=""),
        None,
    )

    assert events == []
    assert diagnostics[-1]["decision_reason"] == "degraded_context"


def test_degraded_diagnostic_retains_capture_error(monkeypatch):
    keyboard_listener, _ = import_keyboard_listener(monkeypatch)
    diagnostics = []
    listener = keyboard_listener.KeyboardListener(
        lambda event: None,
        diagnostics_callback=diagnostics.append,
    )
    listener._get_event_target_app = lambda event: ("Codex", "com.openai.codex")
    monkeypatch.setattr(
        keyboard_listener,
        "capture_accessibility_context",
        lambda: SimpleNamespace(
            focused_role=None,
            focused_subrole=None,
            focused_protected=False,
            capture_status="degraded",
            capture_error="focused element unavailable (system-wide and pid 123)",
        ),
    )
    monkeypatch.setattr(
        keyboard_listener,
        "context_to_dict",
        lambda context: {
            "capture_status": context.capture_status,
            "capture_error": context.capture_error,
        },
    )

    listener._event_callback(
        None,
        keyboard_listener.kCGEventKeyDown,
        SimpleNamespace(keycode=keyboard_listener.ENTER_KEYCODE, text=""),
        None,
    )

    assert diagnostics[-1]["diagnostics"]["capture_error"] == (
        "focused element unavailable (system-wide and pid 123)"
    )


def test_enter_does_not_reuse_recent_snapshot_from_another_field(monkeypatch):
    keyboard_listener, _ = import_keyboard_listener(monkeypatch)
    events = []
    diagnostics = []
    listener = keyboard_listener.KeyboardListener(
        events.append, diagnostics_callback=diagnostics.append
    )
    listener._get_event_target_app = lambda event: ("Codex", "com.openai.codex")
    listener._get_focused_text_snapshot = lambda: ""
    listener._recent_text_snapshots[("Codex", "com.openai.codex")] = (
        "text from first field",
        time.monotonic(),
        "id::first-field",
    )
    monkeypatch.setattr(
        keyboard_listener.config,
        "count_unreadable_submissions",
        False,
        raising=False,
    )
    monkeypatch.setattr(
        keyboard_listener,
        "capture_accessibility_context",
        lambda: SimpleNamespace(
            focused_role="AXTextArea",
            focused_subrole=None,
            focused_identifier="second-field",
            focused_frame=None,
            window_title=None,
            focused_protected=False,
            capture_status="ok",
        ),
    )
    monkeypatch.setattr(keyboard_listener, "context_to_dict", lambda context: {})

    listener._event_callback(
        None,
        keyboard_listener.kCGEventKeyDown,
        SimpleNamespace(keycode=keyboard_listener.ENTER_KEYCODE, text=""),
        None,
    )

    assert events == []
    assert diagnostics[-1]["decision_reason"] == "no_trusted_content"


def test_enter_does_not_reuse_cjk_fallback_from_another_field(monkeypatch):
    keyboard_listener, _ = import_keyboard_listener(monkeypatch)
    events = []
    listener = keyboard_listener.KeyboardListener(events.append)
    listener._get_event_target_app = lambda event: (
        "Claude",
        "com.anthropic.claudefordesktop",
    )
    listener._get_focused_text_snapshot = lambda: "english in second field"
    app_key = ("Claude", "com.anthropic.claudefordesktop")
    listener._active_field_ids[app_key] = "id::first-field"
    listener._text_fallback_buffers[app_key] = ["第一个输入框"]
    listener._text_fallback_buffer_updated_at[app_key] = time.monotonic()
    monkeypatch.setattr(
        keyboard_listener,
        "capture_accessibility_context",
        lambda: SimpleNamespace(
            focused_role="AXTextArea",
            focused_subrole=None,
            focused_identifier="second-field",
            focused_frame=None,
            window_title=None,
            focused_protected=False,
            capture_status="ok",
        ),
    )
    monkeypatch.setattr(keyboard_listener, "context_to_dict", lambda context: {})

    listener._event_callback(
        None,
        keyboard_listener.kCGEventKeyDown,
        SimpleNamespace(keycode=keyboard_listener.ENTER_KEYCODE, text=""),
        None,
    )

    assert len(events) == 1
    assert events[0].character == "english in second field"


def test_ignored_app_never_reads_accessibility(monkeypatch):
    keyboard_listener, _ = import_keyboard_listener(monkeypatch)
    listener = keyboard_listener.KeyboardListener(lambda event: None)
    listener._get_event_target_app = lambda event: (
        "SecurityAgent",
        "com.apple.SecurityAgent",
    )
    listener._capture_focused_context = lambda **kwargs: (_ for _ in ()).throw(
        AssertionError("ignored app must not read Accessibility")
    )

    listener._event_callback(
        None,
        keyboard_listener.kCGEventKeyUp,
        SimpleNamespace(keycode=12, text="x"),
        None,
    )


def test_enter_keyup_can_emit_submission_snapshot(monkeypatch):
    keyboard_listener, _ = import_keyboard_listener(monkeypatch)
    events = []
    listener = keyboard_listener.KeyboardListener(events.append)
    listener._get_event_target_app = lambda event: ("Codex", "com.openai.codex")
    listener._get_focused_text_snapshot = lambda: "需要被记录"
    monkeypatch.setattr(
        keyboard_listener,
        "capture_accessibility_context",
        lambda: SimpleNamespace(focused_role="AXTextArea", focused_subrole=None),
    )
    monkeypatch.setattr(keyboard_listener, "context_to_dict", lambda context: {})

    listener._event_callback(
        None,
        keyboard_listener.kCGEventKeyUp,
        SimpleNamespace(keycode=keyboard_listener.ENTER_KEYCODE),
        None,
    )

    assert len(events) == 1
    assert events[0].character == "需要被记录"
    assert events[0].modifiers["submit_snapshot"] is True


def test_text_entry_enter_saves_english_ax_value_under_chinese_ime(monkeypatch):
    keyboard_listener, _ = import_keyboard_listener(monkeypatch)
    events = []
    listener = keyboard_listener.KeyboardListener(events.append)
    listener._get_event_target_app = lambda event: (
        "Claude",
        "com.anthropic.claudefordesktop",
    )
    listener._get_focused_text_snapshot = lambda: "mixed English input"
    monkeypatch.setattr(
        keyboard_listener,
        "capture_accessibility_context",
        lambda: SimpleNamespace(focused_role="AXTextArea", focused_subrole=None),
    )
    monkeypatch.setattr(keyboard_listener, "context_to_dict", lambda context: {})

    listener._event_callback(
        None,
        keyboard_listener.kCGEventKeyDown,
        SimpleNamespace(keycode=keyboard_listener.ENTER_KEYCODE, text=""),
        None,
    )

    assert len(events) == 1
    assert events[0].character == "mixed English input"
    assert events[0].modifiers["submit_snapshot"] is True
    assert "fallback_source" not in events[0].modifiers


def test_enter_emits_count_only_fallback_by_default_when_ax_value_is_empty(monkeypatch):
    keyboard_listener, _ = import_keyboard_listener(monkeypatch)
    events = []
    listener = keyboard_listener.KeyboardListener(events.append)
    listener._get_event_target_app = lambda event: ("Codex", "com.openai.codex")
    listener._get_focused_text_snapshot = lambda: ""
    monkeypatch.setattr(
        keyboard_listener,
        "capture_accessibility_context",
        lambda: SimpleNamespace(focused_role="AXTextArea", focused_subrole=None),
    )
    monkeypatch.setattr(keyboard_listener, "context_to_dict", lambda context: {})

    listener._event_callback(
        None,
        keyboard_listener.kCGEventKeyDown,
        SimpleNamespace(keycode=12),  # q
        None,
    )
    listener._event_callback(
        None,
        keyboard_listener.kCGEventKeyDown,
        SimpleNamespace(keycode=13),  # w
        None,
    )
    listener._event_callback(
        None,
        keyboard_listener.kCGEventKeyDown,
        SimpleNamespace(keycode=keyboard_listener.ENTER_KEYCODE),
        None,
    )

    assert len(events) == 1
    assert events[0].character == keyboard_listener.UNREADABLE_SUBMISSION_PLACEHOLDER
    assert events[0].modifiers["fallback_source"] == "count_unreadable"
    assert events[0].modifiers["redacted_content"] is True
    assert events[0].modifiers["char_count_override"] == 2


def test_enter_does_not_emit_pinyin_key_event_text_fallback_when_ax_value_is_empty(
    monkeypatch,
):
    keyboard_listener, _ = import_keyboard_listener(monkeypatch)
    events = []
    listener = keyboard_listener.KeyboardListener(events.append)
    listener._get_event_target_app = lambda event: ("Codex", "com.openai.codex")
    listener._get_focused_text_snapshot = lambda: ""
    monkeypatch.setattr(
        keyboard_listener.config,
        "count_unreadable_submissions",
        False,
        raising=False,
    )
    monkeypatch.setattr(
        keyboard_listener,
        "capture_accessibility_context",
        lambda: SimpleNamespace(focused_role="AXTextArea", focused_subrole=None),
    )
    monkeypatch.setattr(keyboard_listener, "context_to_dict", lambda context: {})

    for event in (
        SimpleNamespace(keycode=12, text="p"),
        SimpleNamespace(keycode=34, text="i"),
        SimpleNamespace(keycode=45, text="n"),
        SimpleNamespace(keycode=keyboard_listener.ENTER_KEYCODE, text=""),
    ):
        listener._event_callback(None, keyboard_listener.kCGEventKeyDown, event, None)

    assert events == []


def test_enter_uses_recent_ax_snapshot_when_enter_snapshot_is_empty(monkeypatch):
    keyboard_listener, _ = import_keyboard_listener(monkeypatch)
    events = []
    listener = keyboard_listener.KeyboardListener(events.append)
    listener._get_event_target_app = lambda event: ("Codex", "com.openai.codex")
    listener._get_focused_text_snapshot = lambda: ""
    listener._recent_text_snapshots = {
        ("Codex", "com.openai.codex"): ("你好", time.monotonic(), "id::prompt")
    }
    monkeypatch.setattr(
        keyboard_listener,
        "capture_accessibility_context",
        lambda: SimpleNamespace(
            focused_role="AXTextArea",
            focused_subrole=None,
            focused_identifier="prompt",
            focused_frame=None,
            window_title=None,
        ),
    )
    monkeypatch.setattr(keyboard_listener, "context_to_dict", lambda context: {})

    listener._event_callback(
        None,
        keyboard_listener.kCGEventKeyDown,
        SimpleNamespace(keycode=keyboard_listener.ENTER_KEYCODE, text=""),
        None,
    )

    assert len(events) == 1
    assert events[0].character == "你好"
    assert events[0].modifiers["fallback_source"] == "recent_ax_snapshot"


def test_backspace_keyup_clears_recent_ax_snapshot_when_field_becomes_empty(
    monkeypatch,
):
    keyboard_listener, _ = import_keyboard_listener(monkeypatch)
    events = []
    listener = keyboard_listener.KeyboardListener(events.append)
    listener._get_event_target_app = lambda event: ("Codex", "com.openai.codex")
    listener._get_focused_text_snapshot = lambda: ""
    listener._active_field_ids[("Codex", "com.openai.codex")] = "id::prompt"
    listener._recent_text_snapshots = {
        ("Codex", "com.openai.codex"): ("旧内容", time.monotonic())
    }
    monkeypatch.setattr(
        keyboard_listener,
        "capture_accessibility_context",
        lambda: SimpleNamespace(
            focused_role="AXTextArea",
            focused_subrole=None,
            focused_identifier="prompt",
            focused_frame=None,
            window_title=None,
        ),
    )
    monkeypatch.setattr(keyboard_listener, "context_to_dict", lambda context: {})

    listener._event_callback(
        None,
        keyboard_listener.kCGEventKeyUp,
        SimpleNamespace(keycode=51, text=""),
        None,
    )
    listener._event_callback(
        None,
        keyboard_listener.kCGEventKeyDown,
        SimpleNamespace(keycode=keyboard_listener.ENTER_KEYCODE, text=""),
        None,
    )

    assert events == []


def test_enter_uses_cjk_key_event_text_fallback_by_default_when_ax_value_is_empty(
    monkeypatch,
):
    keyboard_listener, _ = import_keyboard_listener(monkeypatch)
    events = []
    listener = keyboard_listener.KeyboardListener(events.append)
    listener._get_event_target_app = lambda event: ("Codex", "com.openai.codex")
    listener._get_focused_text_snapshot = lambda: ""
    listener._active_field_ids[("Codex", "com.openai.codex")] = "id::prompt"
    monkeypatch.setattr(
        keyboard_listener,
        "capture_accessibility_context",
        lambda: SimpleNamespace(
            focused_role="AXTextArea",
            focused_subrole=None,
            focused_identifier="prompt",
            focused_frame=None,
            window_title=None,
        ),
    )
    monkeypatch.setattr(keyboard_listener, "context_to_dict", lambda context: {})

    listener._event_callback(
        None,
        keyboard_listener.kCGEventKeyDown,
        SimpleNamespace(keycode=12, text="你"),
        None,
    )
    listener._event_callback(
        None,
        keyboard_listener.kCGEventKeyDown,
        SimpleNamespace(keycode=13, text="好"),
        None,
    )
    listener._event_callback(
        None,
        keyboard_listener.kCGEventKeyDown,
        SimpleNamespace(keycode=keyboard_listener.ENTER_KEYCODE, text=""),
        None,
    )

    assert len(events) == 1
    assert events[0].character == "你好"
    assert events[0].modifiers["submit_snapshot"] is True
    assert events[0].modifiers["fallback_source"] == "key_event_text"
    assert events[0].modifiers["redacted_content"] is False
    assert "char_count_override" not in events[0].modifiers


def test_key_event_text_fallback_captures_committed_cjk_from_keyup(monkeypatch):
    keyboard_listener, _ = import_keyboard_listener(monkeypatch)
    events = []
    listener = keyboard_listener.KeyboardListener(events.append)
    listener._get_event_target_app = lambda event: ("Codex", "com.openai.codex")
    listener._get_focused_text_snapshot = lambda: ""
    monkeypatch.setattr(
        keyboard_listener.config,
        "count_unreadable_submissions",
        False,
        raising=False,
    )
    monkeypatch.setattr(
        keyboard_listener,
        "capture_accessibility_context",
        lambda: SimpleNamespace(
            focused_role="AXTextArea",
            focused_subrole=None,
            focused_identifier="prompt",
            focused_frame=None,
            window_title=None,
        ),
    )
    monkeypatch.setattr(keyboard_listener, "context_to_dict", lambda context: {})

    listener._event_callback(
        None,
        keyboard_listener.kCGEventKeyUp,
        SimpleNamespace(keycode=49, text="你好"),
        None,
    )
    listener._event_callback(
        None,
        keyboard_listener.kCGEventKeyDown,
        SimpleNamespace(keycode=keyboard_listener.ENTER_KEYCODE, text=""),
        None,
    )

    assert len(events) == 1
    assert events[0].character == "你好"
    assert events[0].modifiers["fallback_source"] == "key_event_text"


def test_cjk_key_event_fallback_preferred_over_latin_ax_snapshot(monkeypatch):
    keyboard_listener, _ = import_keyboard_listener(monkeypatch)
    events = []
    listener = keyboard_listener.KeyboardListener(events.append)
    listener._get_event_target_app = lambda event: ("Codex", "com.openai.codex")
    listener._get_focused_text_snapshot = lambda: "nihao"
    monkeypatch.setattr(
        keyboard_listener,
        "capture_accessibility_context",
        lambda: SimpleNamespace(
            focused_role="AXTextArea",
            focused_subrole=None,
            focused_identifier="prompt",
            focused_frame=None,
            window_title=None,
        ),
    )
    monkeypatch.setattr(keyboard_listener, "context_to_dict", lambda context: {})

    listener._event_callback(
        None,
        keyboard_listener.kCGEventKeyUp,
        SimpleNamespace(keycode=49, text="你好"),
        None,
    )
    listener._event_callback(
        None,
        keyboard_listener.kCGEventKeyDown,
        SimpleNamespace(keycode=keyboard_listener.ENTER_KEYCODE, text=""),
        None,
    )

    assert len(events) == 1
    assert events[0].character == "你好"
    assert events[0].modifiers["fallback_source"] == "key_event_text"


def test_enter_never_uses_generic_clipboard_when_ax_and_key_text_are_empty(monkeypatch):
    keyboard_listener, _ = import_keyboard_listener(monkeypatch)
    events = []
    listener = keyboard_listener.KeyboardListener(events.append)
    listener._get_event_target_app = lambda event: ("Kim", "Kem")
    listener._get_focused_text_snapshot = lambda: ""
    copy_calls = []
    listener._copy_focused_submission_via_clipboard = (
        lambda app_name, bundle_id, fallback_count=0: (
            copy_calls.append((app_name, bundle_id, fallback_count)) or "今天记录中文"
        )
    )
    listener._can_use_clipboard_copy_fallback = lambda: True
    monkeypatch.setattr(
        keyboard_listener.config,
        "count_unreadable_submissions",
        False,
        raising=False,
    )
    monkeypatch.setattr(
        keyboard_listener,
        "capture_accessibility_context",
        lambda: SimpleNamespace(focused_role="AXTextArea", focused_subrole=None),
    )
    monkeypatch.setattr(keyboard_listener, "context_to_dict", lambda context: {})

    listener._event_callback(
        None,
        keyboard_listener.kCGEventKeyDown,
        SimpleNamespace(keycode=49, text=" "),
        None,
    )
    listener._event_callback(
        None,
        keyboard_listener.kCGEventKeyDown,
        SimpleNamespace(keycode=keyboard_listener.ENTER_KEYCODE, text=""),
        None,
    )

    assert events == []
    assert copy_calls == []


def test_enter_without_recent_input_does_not_use_clipboard_copy_fallback(monkeypatch):
    keyboard_listener, _ = import_keyboard_listener(monkeypatch)
    events = []
    diagnostics = []
    copy_calls = []
    listener = keyboard_listener.KeyboardListener(
        events.append, diagnostics_callback=diagnostics.append
    )
    listener._get_event_target_app = lambda event: (
        "Claude",
        "com.anthropic.claudefordesktop",
    )
    listener._get_focused_text_snapshot = lambda: ""
    listener._can_use_clipboard_copy_fallback = lambda: True
    monkeypatch.setattr(
        keyboard_listener.config,
        "count_unreadable_submissions",
        False,
        raising=False,
    )

    def copy_fallback(app_name, bundle_id, fallback_count=0):
        copy_calls.append((app_name, bundle_id, fallback_count))
        return "whole conversation copied"

    listener._copy_focused_submission_via_clipboard = copy_fallback
    monkeypatch.setattr(
        keyboard_listener,
        "capture_accessibility_context",
        lambda: SimpleNamespace(focused_role="AXTextArea", focused_subrole=None),
    )
    monkeypatch.setattr(keyboard_listener, "context_to_dict", lambda context: {})

    listener._event_callback(
        None,
        keyboard_listener.kCGEventKeyDown,
        SimpleNamespace(keycode=keyboard_listener.ENTER_KEYCODE, text=""),
        None,
    )

    assert events == []
    assert copy_calls == []
    assert diagnostics[-1]["decision_action"] == "skip"
    assert diagnostics[-1]["decision_reason"] == "no_trusted_content"


def test_enter_in_non_text_context_does_not_save_ax_page_value(monkeypatch):
    keyboard_listener, _ = import_keyboard_listener(monkeypatch)
    events = []
    diagnostics = []
    listener = keyboard_listener.KeyboardListener(
        events.append, diagnostics_callback=diagnostics.append
    )
    listener._get_event_target_app = lambda event: (
        "Claude",
        "com.anthropic.claudefordesktop",
    )
    listener._get_focused_text_snapshot = lambda: "whole conversation page text"
    monkeypatch.setattr(
        keyboard_listener,
        "capture_accessibility_context",
        lambda: SimpleNamespace(
            focused_role="AXGroup", focused_subrole=None, capture_status="ok"
        ),
    )
    monkeypatch.setattr(
        keyboard_listener,
        "context_to_dict",
        lambda context: {
            "focused_role": context.focused_role,
            "focused_subrole": context.focused_subrole,
            "capture_status": context.capture_status,
        },
    )

    listener._event_callback(
        None,
        keyboard_listener.kCGEventKeyDown,
        SimpleNamespace(keycode=keyboard_listener.ENTER_KEYCODE, text=""),
        None,
    )

    assert events == []
    assert diagnostics[-1]["decision_action"] == "skip"
    assert diagnostics[-1]["decision_reason"] == "focused_element_not_text_input"
    assert diagnostics[-1]["focused_role"] == "AXGroup"


def test_enter_in_non_text_context_does_not_use_clipboard_copy_fallback(monkeypatch):
    keyboard_listener, _ = import_keyboard_listener(monkeypatch)
    events = []
    diagnostics = []
    copy_calls = []
    listener = keyboard_listener.KeyboardListener(
        events.append, diagnostics_callback=diagnostics.append
    )
    listener._get_event_target_app = lambda event: (
        "Claude",
        "com.anthropic.claudefordesktop",
    )
    listener._get_focused_text_snapshot = lambda: ""
    listener._can_use_clipboard_copy_fallback = lambda: True
    listener._copy_focused_submission_via_clipboard = (
        lambda app_name, bundle_id, fallback_count=0: (
            copy_calls.append((app_name, bundle_id, fallback_count))
            or "whole conversation copied"
        )
    )
    monkeypatch.setattr(
        keyboard_listener,
        "capture_accessibility_context",
        lambda: SimpleNamespace(
            focused_role="AXWebArea", focused_subrole=None, capture_status="ok"
        ),
    )
    monkeypatch.setattr(
        keyboard_listener,
        "context_to_dict",
        lambda context: {
            "focused_role": context.focused_role,
            "focused_subrole": context.focused_subrole,
            "capture_status": context.capture_status,
        },
    )

    listener._event_callback(
        None,
        keyboard_listener.kCGEventKeyDown,
        SimpleNamespace(keycode=49, text=" "),
        None,
    )
    listener._event_callback(
        None,
        keyboard_listener.kCGEventKeyDown,
        SimpleNamespace(keycode=keyboard_listener.ENTER_KEYCODE, text=""),
        None,
    )

    assert events == []
    assert copy_calls == []
    assert diagnostics[-1]["decision_action"] == "skip"
    assert diagnostics[-1]["decision_reason"] == "focused_element_not_text_input"
    assert diagnostics[-1]["focused_role"] == "AXWebArea"


def test_count_only_fallback_does_not_invoke_generic_clipboard(monkeypatch):
    keyboard_listener, _ = import_keyboard_listener(monkeypatch)
    events = []
    listener = keyboard_listener.KeyboardListener(events.append)
    listener._get_event_target_app = lambda event: ("Kim", "Kem")
    listener._get_focused_text_snapshot = lambda: ""
    copy_calls = []
    listener._copy_focused_submission_via_clipboard = (
        lambda app_name, bundle_id, fallback_count=0: (
            copy_calls.append((app_name, bundle_id, fallback_count)) or "输入框原文"
        )
    )
    listener._can_use_clipboard_copy_fallback = lambda: True
    monkeypatch.setattr(
        keyboard_listener,
        "capture_accessibility_context",
        lambda: SimpleNamespace(focused_role="AXTextArea", focused_subrole=None),
    )
    monkeypatch.setattr(keyboard_listener, "context_to_dict", lambda context: {})

    listener._event_callback(
        None,
        keyboard_listener.kCGEventKeyDown,
        SimpleNamespace(keycode=49, text=" "),
        None,
    )
    listener._event_callback(
        None,
        keyboard_listener.kCGEventKeyDown,
        SimpleNamespace(keycode=keyboard_listener.ENTER_KEYCODE, text=""),
        None,
    )

    assert len(events) == 1
    assert events[0].character == keyboard_listener.UNREADABLE_SUBMISSION_PLACEHOLDER
    assert events[0].modifiers["fallback_source"] == "count_unreadable"
    assert events[0].modifiers["redacted_content"] is True
    assert events[0].modifiers["char_count_override"] == 1
    assert copy_calls == []


def test_generic_clipboard_is_skipped_and_count_only_fallback_is_used(monkeypatch):
    keyboard_listener, _ = import_keyboard_listener(monkeypatch)
    events = []
    diagnostics = []
    copy_calls = []
    listener = keyboard_listener.KeyboardListener(
        events.append, diagnostics_callback=diagnostics.append
    )
    listener._get_event_target_app = lambda event: (
        "Claude",
        "com.anthropic.claudefordesktop",
    )
    listener._get_focused_text_snapshot = lambda: ""
    listener._can_use_clipboard_copy_fallback = lambda: True

    def copy_fallback(app_name, bundle_id, fallback_count=0):
        copy_calls.append((app_name, bundle_id, fallback_count))
        return ""

    listener._copy_focused_submission_via_clipboard = copy_fallback
    monkeypatch.setattr(
        keyboard_listener,
        "capture_accessibility_context",
        lambda: SimpleNamespace(focused_role="AXTextArea", focused_subrole=None),
    )
    monkeypatch.setattr(keyboard_listener, "context_to_dict", lambda context: {})

    listener._event_callback(
        None,
        keyboard_listener.kCGEventKeyDown,
        SimpleNamespace(keycode=49, text=" "),
        None,
    )
    listener._event_callback(
        None,
        keyboard_listener.kCGEventKeyDown,
        SimpleNamespace(keycode=keyboard_listener.ENTER_KEYCODE, text=""),
        None,
    )

    assert len(events) == 1
    assert events[0].modifiers["fallback_source"] == "count_unreadable"
    assert copy_calls == []
    assert diagnostics == []


def test_latin_ime_empty_ax_never_enables_later_clipboard_capture(monkeypatch):
    keyboard_listener, _ = import_keyboard_listener(monkeypatch)
    events = []
    copy_calls = []
    listener = keyboard_listener.KeyboardListener(events.append)
    listener._get_event_target_app = lambda event: ("Kim", "Kem")
    listener._get_focused_text_snapshot = lambda: ""
    listener._can_use_clipboard_copy_fallback = lambda: True
    monkeypatch.setattr(
        keyboard_listener.config,
        "count_unreadable_submissions",
        False,
        raising=False,
    )

    def copy_fallback(app_name, bundle_id, fallback_count=0):
        copy_calls.append((app_name, bundle_id, fallback_count))
        return "mixed English"

    listener._copy_focused_submission_via_clipboard = copy_fallback
    monkeypatch.setattr(
        keyboard_listener,
        "capture_accessibility_context",
        lambda: SimpleNamespace(focused_role="AXTextArea", focused_subrole=None),
    )
    monkeypatch.setattr(keyboard_listener, "context_to_dict", lambda context: {})

    for event in (
        SimpleNamespace(keycode=46, text="m"),
        SimpleNamespace(keycode=34, text="i"),
        SimpleNamespace(keycode=6, text="x"),
    ):
        listener._event_callback(None, keyboard_listener.kCGEventKeyDown, event, None)

    listener._event_callback(
        None,
        keyboard_listener.kCGEventKeyDown,
        SimpleNamespace(keycode=keyboard_listener.ENTER_KEYCODE, text=""),
        None,
    )
    listener._event_callback(
        None,
        keyboard_listener.kCGEventKeyUp,
        SimpleNamespace(keycode=keyboard_listener.ENTER_KEYCODE, text=""),
        None,
    )

    assert events == []
    assert copy_calls == []

    listener._event_callback(
        None,
        keyboard_listener.kCGEventKeyDown,
        SimpleNamespace(keycode=keyboard_listener.ENTER_KEYCODE, text=""),
        None,
    )

    assert events == []
    assert copy_calls == []


def test_latin_ime_clipboard_allowance_bounds_copied_content(monkeypatch):
    keyboard_listener, _ = import_keyboard_listener(monkeypatch)
    events = []
    listener = keyboard_listener.KeyboardListener(events.append)
    listener._get_event_target_app = lambda event: (
        "Claude",
        "com.anthropic.claudefordesktop",
    )
    listener._get_focused_text_snapshot = lambda: ""
    listener._can_use_clipboard_copy_fallback = lambda: True
    listener._snapshot_general_pasteboard = lambda: None
    listener._restore_general_pasteboard = lambda snapshot: None
    listener._post_command_key = lambda keycode: True
    listener._post_plain_key = lambda keycode: True
    listener._read_general_pasteboard_text = lambda: "x" * 1000
    monkeypatch.setattr(keyboard_listener.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        keyboard_listener.config,
        "count_unreadable_submissions",
        False,
        raising=False,
    )
    monkeypatch.setattr(
        keyboard_listener,
        "capture_accessibility_context",
        lambda: SimpleNamespace(focused_role="AXTextArea", focused_subrole=None),
    )
    monkeypatch.setattr(keyboard_listener, "context_to_dict", lambda context: {})

    for event in (
        SimpleNamespace(keycode=46, text="m"),
        SimpleNamespace(keycode=34, text="i"),
        SimpleNamespace(keycode=6, text="x"),
    ):
        listener._event_callback(None, keyboard_listener.kCGEventKeyDown, event, None)

    listener._event_callback(
        None,
        keyboard_listener.kCGEventKeyDown,
        SimpleNamespace(keycode=keyboard_listener.ENTER_KEYCODE, text=""),
        None,
    )
    listener._event_callback(
        None,
        keyboard_listener.kCGEventKeyUp,
        SimpleNamespace(keycode=keyboard_listener.ENTER_KEYCODE, text=""),
        None,
    )
    listener._event_callback(
        None,
        keyboard_listener.kCGEventKeyDown,
        SimpleNamespace(keycode=keyboard_listener.ENTER_KEYCODE, text=""),
        None,
    )

    assert events == []


def test_key_event_text_fallback_drops_pinyin_prefix_before_committed_cjk(monkeypatch):
    keyboard_listener, _ = import_keyboard_listener(monkeypatch)
    events = []
    listener = keyboard_listener.KeyboardListener(events.append)
    listener._get_event_target_app = lambda event: ("Codex", "com.openai.codex")
    listener._get_focused_text_snapshot = lambda: ""
    listener._active_field_ids[("Codex", "com.openai.codex")] = "id::prompt"
    monkeypatch.setattr(
        keyboard_listener,
        "capture_accessibility_context",
        lambda: SimpleNamespace(
            focused_role="AXTextArea",
            focused_subrole=None,
            focused_identifier="prompt",
            focused_frame=None,
            window_title=None,
        ),
    )
    monkeypatch.setattr(keyboard_listener, "context_to_dict", lambda context: {})

    for event in (
        SimpleNamespace(keycode=12, text="p"),
        SimpleNamespace(keycode=34, text="i"),
        SimpleNamespace(keycode=45, text="n"),
        SimpleNamespace(keycode=12, text="你"),
        SimpleNamespace(keycode=13, text="好"),
        SimpleNamespace(keycode=keyboard_listener.ENTER_KEYCODE, text=""),
    ):
        listener._event_callback(None, keyboard_listener.kCGEventKeyDown, event, None)

    assert len(events) == 1
    assert events[0].character == "你好"


def test_key_event_text_fallback_handles_backspace(monkeypatch):
    keyboard_listener, _ = import_keyboard_listener(monkeypatch)
    events = []
    listener = keyboard_listener.KeyboardListener(events.append)
    listener._get_event_target_app = lambda event: ("Codex", "com.openai.codex")
    listener._get_focused_text_snapshot = lambda: ""
    listener._active_field_ids[("Codex", "com.openai.codex")] = "id::prompt"
    monkeypatch.setattr(
        keyboard_listener.config,
        "capture_key_event_text_fallback",
        True,
        raising=False,
    )
    monkeypatch.setattr(
        keyboard_listener,
        "capture_accessibility_context",
        lambda: SimpleNamespace(
            focused_role="AXTextArea",
            focused_subrole=None,
            focused_identifier="prompt",
            focused_frame=None,
            window_title=None,
        ),
    )
    monkeypatch.setattr(keyboard_listener, "context_to_dict", lambda context: {})

    for event in (
        SimpleNamespace(keycode=0, text="你"),
        SimpleNamespace(keycode=11, text="好"),
        SimpleNamespace(keycode=51, text=""),
        SimpleNamespace(keycode=8, text="吗"),
        SimpleNamespace(keycode=keyboard_listener.ENTER_KEYCODE, text=""),
    ):
        listener._event_callback(None, keyboard_listener.kCGEventKeyDown, event, None)

    assert len(events) == 1
    assert events[0].character == "你吗"


def test_enter_does_not_emit_count_only_fallback_when_disabled(monkeypatch):
    keyboard_listener, _ = import_keyboard_listener(monkeypatch)
    events = []
    listener = keyboard_listener.KeyboardListener(events.append)
    listener._get_event_target_app = lambda event: ("Codex", "com.openai.codex")
    listener._get_focused_text_snapshot = lambda: ""
    monkeypatch.setattr(
        keyboard_listener.config,
        "count_unreadable_submissions",
        False,
        raising=False,
    )
    monkeypatch.setattr(
        keyboard_listener,
        "capture_accessibility_context",
        lambda: SimpleNamespace(focused_role="AXTextArea", focused_subrole=None),
    )
    monkeypatch.setattr(keyboard_listener, "context_to_dict", lambda context: {})

    listener._event_callback(
        None,
        keyboard_listener.kCGEventKeyDown,
        SimpleNamespace(keycode=12),  # q
        None,
    )
    listener._event_callback(
        None,
        keyboard_listener.kCGEventKeyDown,
        SimpleNamespace(keycode=13),  # w
        None,
    )
    listener._event_callback(
        None,
        keyboard_listener.kCGEventKeyDown,
        SimpleNamespace(keycode=keyboard_listener.ENTER_KEYCODE),
        None,
    )

    assert events == []


class FakeDoubaoCandidateReader:
    def __init__(self, snapshots=(), on_read=None):
        self.snapshots = list(snapshots)
        self.on_read = on_read
        self.calls = []
        self.last_failure_reason = None

    def read(self, *, target_pid, target_bundle_id):
        self.calls.append((target_pid, target_bundle_id))
        if self.on_read is not None:
            self.on_read()
        if self.snapshots:
            snapshot = self.snapshots.pop(0)
            self.last_failure_reason = (
                None if snapshot is not None else "candidate_ax_unavailable"
            )
            return snapshot
        self.last_failure_reason = "candidate_ax_unavailable"
        return None


def doubao_raw_event(
    keyboard_listener,
    event_type,
    keycode,
    text="",
    *,
    target_pid=123,
    modifiers=None,
    is_autorepeat=False,
):
    return keyboard_listener.RawKeyboardEvent(
        event_type=event_type,
        keycode=keycode,
        text=text,
        app_name="Kim",
        bundle_id="Kem",
        modifiers=modifiers
        or {"shift": False, "ctrl": False, "alt": False, "cmd": False},
        target_pid=target_pid,
        is_autorepeat=is_autorepeat,
    )


def test_doubao_candidate_commit_becomes_postsend_validation(monkeypatch):
    keyboard_listener, _ = import_keyboard_listener(monkeypatch)
    snapshot = CandidateSnapshot(("测试", "策士"), 123, time.monotonic())
    reader = FakeDoubaoCandidateReader([snapshot])
    coordinator = FakePostSendCoordinator()
    listener = keyboard_listener.KeyboardListener(
        lambda event: None,
        candidate_reader=reader,
        post_send_coordinator=coordinator,
        baseline_sampler=FakeBaselineSampler(),
    )
    configure_listener_context(listener)
    monkeypatch.setattr(keyboard_listener, "get_app_by_pid", lambda pid: ("Kim", "Kem"))

    for raw_event in (
        doubao_raw_event(keyboard_listener, keyboard_listener.kCGEventKeyDown, 8, "c"),
        doubao_raw_event(keyboard_listener, keyboard_listener.kCGEventKeyDown, 49, " "),
        doubao_raw_event(
            keyboard_listener,
            keyboard_listener.kCGEventKeyDown,
            keyboard_listener.ENTER_KEYCODE,
        ),
    ):
        listener._process_raw_event(raw_event)

    assert reader.calls == [(123, "Kem")]
    assert coordinator.submitted[0].validation_text == "测试"


def test_doubao_number_commit_preserves_confirmed_prefix(monkeypatch):
    keyboard_listener, _ = import_keyboard_listener(monkeypatch)
    reader = FakeDoubaoCandidateReader(
        [
            CandidateSnapshot(("测试", "策士"), 123, time.monotonic()),
            CandidateSnapshot(("你好", "拟好"), 123, time.monotonic()),
        ]
    )
    listener = keyboard_listener.KeyboardListener(
        lambda event: None, candidate_reader=reader
    )
    listener._record_recent_text_snapshot = lambda *args, **kwargs: None
    monkeypatch.setattr(keyboard_listener, "get_app_by_pid", lambda pid: ("Kim", "Kem"))

    for raw_event in (
        doubao_raw_event(keyboard_listener, keyboard_listener.kCGEventKeyDown, 8, "c"),
        doubao_raw_event(keyboard_listener, keyboard_listener.kCGEventKeyDown, 49, " "),
        doubao_raw_event(keyboard_listener, keyboard_listener.kCGEventKeyDown, 45, "n"),
        doubao_raw_event(keyboard_listener, keyboard_listener.kCGEventKeyDown, 19, "2"),
    ):
        listener._process_raw_event(raw_event)

    assert listener._doubao_states[("Kim", "Kem")].confirmed_text == "测试拟好"


def test_doubao_arrow_autorepeat_uses_one_candidate_read(monkeypatch):
    keyboard_listener, _ = import_keyboard_listener(monkeypatch)
    snapshot = CandidateSnapshot(("第一", "第二", "第三"), 123, time.monotonic())
    reader = FakeDoubaoCandidateReader([snapshot])
    listener = keyboard_listener.KeyboardListener(
        lambda event: None, candidate_reader=reader
    )
    listener._record_recent_text_snapshot = lambda *args, **kwargs: None
    monkeypatch.setattr(keyboard_listener, "get_app_by_pid", lambda pid: ("Kim", "Kem"))

    listener._process_raw_event(
        doubao_raw_event(keyboard_listener, keyboard_listener.kCGEventKeyDown, 8, "c")
    )
    listener._process_raw_event(
        doubao_raw_event(keyboard_listener, keyboard_listener.kCGEventKeyDown, 124)
    )
    for _ in range(32):
        listener._process_raw_event(
            doubao_raw_event(
                keyboard_listener,
                keyboard_listener.kCGEventKeyDown,
                124,
                is_autorepeat=True,
            )
        )

    assert reader.calls == [(123, "Kem")]
    assert listener._doubao_states[("Kim", "Kem")].selected_index == 2


@pytest.mark.parametrize("clear_kind", ("pid", "app", "shortcut", "mouse"))
def test_doubao_composition_is_cleared_on_context_change(monkeypatch, clear_kind):
    keyboard_listener, _ = import_keyboard_listener(monkeypatch)
    snapshot = CandidateSnapshot(("测试",), 123, time.monotonic())
    listener = keyboard_listener.KeyboardListener(
        lambda event: None,
        candidate_reader=FakeDoubaoCandidateReader([snapshot]),
    )
    listener._record_recent_text_snapshot = lambda *args, **kwargs: None
    monkeypatch.setattr(
        keyboard_listener,
        "get_app_by_pid",
        lambda pid: ("Kim", "Kem") if pid == 123 else ("Codex", "com.openai.codex"),
    )
    listener._process_raw_event(
        doubao_raw_event(keyboard_listener, keyboard_listener.kCGEventKeyDown, 8, "c")
    )
    listener._process_raw_event(
        doubao_raw_event(keyboard_listener, keyboard_listener.kCGEventKeyDown, 49, " ")
    )
    assert listener._doubao_states

    if clear_kind == "pid":
        event = doubao_raw_event(
            keyboard_listener,
            keyboard_listener.kCGEventKeyDown,
            7,
            "x",
            target_pid=456,
        )
    elif clear_kind == "app":
        event = keyboard_listener.RawKeyboardEvent(
            event_type=keyboard_listener.kCGEventKeyDown,
            keycode=0,
            text="a",
            app_name="Codex",
            bundle_id="com.openai.codex",
            modifiers={"shift": False, "ctrl": False, "alt": False, "cmd": False},
            target_pid=999,
        )
    elif clear_kind == "shortcut":
        event = doubao_raw_event(
            keyboard_listener,
            keyboard_listener.kCGEventKeyDown,
            9,
            "v",
            modifiers={"shift": False, "ctrl": False, "alt": False, "cmd": True},
        )
    else:
        event = doubao_raw_event(
            keyboard_listener,
            keyboard_listener.kCGEventLeftMouseDown,
            0,
        )

    listener._process_raw_event(event)

    assert listener._doubao_states == {}


def test_doubao_secure_enter_discards_candidate(monkeypatch):
    keyboard_listener, _ = import_keyboard_listener(monkeypatch)
    snapshot = CandidateSnapshot(("秘密",), 123, time.monotonic())
    coordinator = FakePostSendCoordinator()
    listener = keyboard_listener.KeyboardListener(
        lambda event: None,
        candidate_reader=FakeDoubaoCandidateReader([snapshot]),
        post_send_coordinator=coordinator,
        baseline_sampler=FakeBaselineSampler(),
    )
    configure_listener_context(listener, status="ok", secure=True)
    monkeypatch.setattr(keyboard_listener, "get_app_by_pid", lambda pid: ("Kim", "Kem"))
    for raw_event in (
        doubao_raw_event(keyboard_listener, keyboard_listener.kCGEventKeyDown, 8, "m"),
        doubao_raw_event(keyboard_listener, keyboard_listener.kCGEventKeyDown, 49, " "),
        doubao_raw_event(
            keyboard_listener,
            keyboard_listener.kCGEventKeyDown,
            keyboard_listener.ENTER_KEYCODE,
        ),
    ):
        listener._process_raw_event(raw_event)

    assert coordinator.submitted == []
    assert listener._doubao_states == {}


def test_non_ocr_keyup_keeps_snapshot_and_candidate_refresh(monkeypatch):
    keyboard_listener, _ = import_keyboard_listener(monkeypatch)
    listener = keyboard_listener.KeyboardListener(lambda event: None)
    snapshot_calls = []
    candidate_calls = []
    listener._record_recent_text_snapshot = lambda *args, **kwargs: (
        snapshot_calls.append((args, kwargs))
    )
    listener._refresh_doubao_candidates = lambda *args, **kwargs: (
        candidate_calls.append((args, kwargs))
    )
    monkeypatch.setattr(
        keyboard_listener,
        "get_app_by_pid",
        lambda pid: ("Codex", "com.openai.codex"),
    )

    listener._process_raw_event(
        keyboard_listener.RawKeyboardEvent(
            event_type=keyboard_listener.kCGEventKeyUp,
            keycode=8,
            text="c",
            app_name="Codex",
            bundle_id="com.openai.codex",
            modifiers={
                "shift": False,
                "ctrl": False,
                "alt": False,
                "cmd": False,
            },
            target_pid=123,
        )
    )

    assert snapshot_calls == [
        (("Codex", "com.openai.codex"), {"clear_on_empty": False})
    ]
    assert candidate_calls == [
        (
            (
                "Codex",
                "com.openai.codex",
                123,
                8,
                {
                    "shift": False,
                    "ctrl": False,
                    "alt": False,
                    "cmd": False,
                },
            ),
            {},
        )
    ]


def configure_listener_context(listener, *, status="degraded", secure=False):
    listener._record_recent_text_snapshot = lambda *args, **kwargs: None
    listener._capture_focused_context = lambda **kwargs: SimpleNamespace(
        focused_role="AXTextField" if secure else None,
        focused_subrole="AXSecureTextField" if secure else None,
        focused_protected=secure,
        capture_status=status,
        capture_error=None if secure else "focused element unavailable",
    )
    listener._context_to_dict_safe = lambda context: {
        "focused_role": context.focused_role,
        "focused_subrole": context.focused_subrole,
        "focused_protected": context.focused_protected,
        "capture_status": context.capture_status,
        "capture_error": context.capture_error,
    }


def test_safari_enter_uses_verified_ax_range_when_ax_value_is_empty(monkeypatch):
    keyboard_listener, _ = import_keyboard_listener(monkeypatch)
    from ominime.context_capture import CapturedContext, focused_field_identity

    events = []
    listener = keyboard_listener.KeyboardListener(events.append)
    context = CapturedContext(
        focused_role="AXTextField",
        focused_identifier="prompt-test",
        window_title="Test",
        focused_value="",
        capture_status="ok",
    )
    app_name, bundle_id = "Safari", "com.apple.Safari"
    key = listener._fallback_buffer_key(app_name, bundle_id)
    listener._active_field_ids[key] = focused_field_identity(context)
    calls = []
    monkeypatch.setattr(
        keyboard_listener, "read_focused_text_via_range",
        lambda pid, max_chars: calls.append((pid, max_chars)) or "今天天气不错",
    )

    listener._emit_submission_snapshot(
        None, app_name=app_name, bundle_id=bundle_id,
        event_type=keyboard_listener.kCGEventKeyDown, target_pid=4321,
        captured_context=context,
    )

    assert calls == [(4321, keyboard_listener.MAX_TRUSTED_SUBMISSION_CHARS)]
    assert len(events) == 1
    assert events[0].character == "今天天气不错"
    assert events[0].modifiers["fallback_source"] == "ax_range"
    assert events[0].modifiers["capture_diagnostics"]["ax_range_available"] is True


def test_safari_range_fallback_refuses_unverified_field_switch(monkeypatch):
    keyboard_listener, _ = import_keyboard_listener(monkeypatch)
    from ominime.context_capture import CapturedContext

    events = []
    diagnostics = []
    listener = keyboard_listener.KeyboardListener(
        events.append, diagnostics_callback=diagnostics.append
    )
    context = CapturedContext(
        focused_role="AXTextField",
        focused_identifier="different-field",
        window_title="Test",
        focused_value="",
        capture_status="ok",
    )
    key = listener._fallback_buffer_key("Safari", "com.apple.Safari")
    listener._active_field_ids[key] = "id:Test:previous-field"

    def forbidden_range(*args, **kwargs):
        raise AssertionError("must not copy text from an unverified field")

    monkeypatch.setattr(keyboard_listener, "read_focused_text_via_range", forbidden_range)
    listener._emit_submission_snapshot(
        None, app_name="Safari", bundle_id="com.apple.Safari",
        event_type=keyboard_listener.kCGEventKeyDown, target_pid=4321,
        captured_context=context,
    )
    assert events == []
    assert diagnostics[-1]["decision_reason"] == "no_trusted_content"
