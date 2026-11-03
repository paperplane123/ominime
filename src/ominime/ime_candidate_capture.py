"""Capture and model trusted IME candidate selections for registered input methods."""

import time
import ctypes
import threading
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Callable, Iterable


DOUBAO_BUNDLE_ID = "com.bytedance.inputmethod.doubaoime"
COMPOSER_TOP_FRACTION = 0.55
MAX_COMPOSED_CHARS = 4000
INPUT_SOURCE_CACHE_TTL_SECONDS = 1.0
_input_source_snapshot = ("", 0.0)
_input_source_snapshot_lock = threading.Lock()
NUMBER_KEYCODE_TO_INDEX = {
    18: 0,
    19: 1,
    20: 2,
    21: 3,
    23: 4,
    22: 5,
    26: 6,
    28: 7,
    25: 8,
}


@dataclass(frozen=True)
class ImeCandidateProfile:
    """One registered input method whose candidate window can be read."""

    name: str
    bundle_id: str


IME_CANDIDATE_PROFILES = {
    DOUBAO_BUNDLE_ID: ImeCandidateProfile(name="doubao", bundle_id=DOUBAO_BUNDLE_ID),
}


def default_ime_profile_resolver(input_source_bundle_id: str) -> ImeCandidateProfile | None:
    """Resolve the active input source against the registered profile table.

    A new input method (for example the WeChat IME) joins this registry only
    after the local probe proves its candidate window exposes the same
    AXStaticText + window-bounds contract that the composer geometry gate
    requires.
    """
    return IME_CANDIDATE_PROFILES.get(input_source_bundle_id or "")


@dataclass(frozen=True)
class Rect:
    x: float
    y: float
    width: float
    height: float


@dataclass(frozen=True)
class CandidateSnapshot:
    candidates: tuple[str, ...]
    target_pid: int
    captured_at: float


@dataclass(frozen=True)
class WindowRecord:
    pid: int
    bounds: Rect


@dataclass(frozen=True)
class CompositionResult:
    candidate_committed: bool = False
    committed_text: str = ""


def candidate_is_in_composer(candidate: Rect, target: Rect) -> bool:
    """Return whether a candidate window is anchored in the target composer."""
    if min(candidate.width, candidate.height, target.width, target.height) <= 0:
        return False
    if candidate.width > target.width or candidate.height > target.height * 0.25:
        return False
    center_x = candidate.x + candidate.width / 2
    center_y = candidate.y + candidate.height / 2
    return (
        target.x <= center_x <= target.x + target.width
        and target.y + target.height * COMPOSER_TOP_FRACTION
        <= center_y
        <= target.y + target.height
    )


def rect_from_window_bounds(bounds) -> Rect | None:
    """Convert Python or Objective-C window-bound mappings to a Rect."""
    getter = getattr(bounds, "get", None)
    if getter is None:
        return None
    try:
        return Rect(
            x=float(getter("X", getter("x"))),
            y=float(getter("Y", getter("y"))),
            width=float(getter("Width", getter("width"))),
            height=float(getter("Height", getter("height"))),
        )
    except (TypeError, ValueError):
        return None


def collect_static_text_values(
    roots: Iterable[object],
    attribute_reader: Callable[[object, str], object],
    *,
    max_nodes: int = 200,
) -> tuple[str, ...]:
    """Collect bounded candidate strings from an Accessibility subtree."""
    pending = deque(roots)
    visited: set[int] = set()
    values: list[str] = []
    seen_values: set[str] = set()
    processed = 0
    while pending and processed < max_nodes:
        node = pending.popleft()
        identity = id(node)
        if identity in visited:
            continue
        visited.add(identity)
        processed += 1
        if attribute_reader(node, "AXRole") == "AXStaticText":
            value = attribute_reader(node, "AXValue")
            if isinstance(value, str):
                value = value.strip()
                if value and value not in seen_values:
                    values.append(value)
                    seen_values.add(value)
        pending.extend(object_sequence(attribute_reader(node, "AXChildren")))
    return tuple(values)


def object_sequence(value) -> tuple[object, ...]:
    """Normalize Python and Objective-C array proxies without accepting text."""
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        return ()
    return tuple(value)


@lru_cache(maxsize=1)
def _native_input_source_api():
    hitoolbox = ctypes.CDLL(
        "/System/Library/Frameworks/Carbon.framework/Frameworks/"
        "HIToolbox.framework/HIToolbox"
    )
    core_foundation = ctypes.CDLL(
        "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
    )
    hitoolbox.TISCopyCurrentKeyboardInputSource.restype = ctypes.c_void_p
    hitoolbox.TISGetInputSourceProperty.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    hitoolbox.TISGetInputSourceProperty.restype = ctypes.c_void_p
    core_foundation.CFStringGetLength.argtypes = [ctypes.c_void_p]
    core_foundation.CFStringGetLength.restype = ctypes.c_long
    core_foundation.CFStringGetMaximumSizeForEncoding.argtypes = [
        ctypes.c_long,
        ctypes.c_uint32,
    ]
    core_foundation.CFStringGetMaximumSizeForEncoding.restype = ctypes.c_long
    core_foundation.CFStringGetCString.argtypes = [
        ctypes.c_void_p,
        ctypes.c_char_p,
        ctypes.c_long,
        ctypes.c_uint32,
    ]
    core_foundation.CFStringGetCString.restype = ctypes.c_bool
    core_foundation.CFRelease.argtypes = [ctypes.c_void_p]
    bundle_key = ctypes.c_void_p.in_dll(
        hitoolbox,
        "kTISPropertyBundleID",
    ).value
    return hitoolbox, core_foundation, bundle_key


def current_input_source_bundle_id() -> str:
    """Return the exact bundle identifier of the active macOS input source."""
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("macOS input source must be read on the main thread")
    hitoolbox, core_foundation, bundle_key = _native_input_source_api()
    source = hitoolbox.TISCopyCurrentKeyboardInputSource()
    if not source:
        return ""
    try:
        value = hitoolbox.TISGetInputSourceProperty(source, bundle_key)
        if not value:
            return ""
        encoding_utf8 = 0x08000100
        length = core_foundation.CFStringGetLength(value)
        size = (
            core_foundation.CFStringGetMaximumSizeForEncoding(
                length,
                encoding_utf8,
            )
            + 1
        )
        buffer = ctypes.create_string_buffer(size)
        if not core_foundation.CFStringGetCString(
            value,
            buffer,
            size,
            encoding_utf8,
        ):
            return ""
        return buffer.value.decode("utf-8")
    finally:
        core_foundation.CFRelease(source)


def refresh_input_source_cache(
    *,
    native_provider: Callable[[], str] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> str:
    """Refresh the active input source snapshot from the Python main thread."""
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("input source cache must be refreshed on the main thread")
    provider = native_provider or current_input_source_bundle_id
    try:
        bundle_id = provider()
    except Exception:
        bundle_id = ""
    if not isinstance(bundle_id, str):
        bundle_id = ""
    snapshot = (bundle_id, clock())
    global _input_source_snapshot
    with _input_source_snapshot_lock:
        _input_source_snapshot = snapshot
    return bundle_id


def cached_input_source_bundle_id(
    *,
    clock: Callable[[], float] = time.monotonic,
) -> str:
    """Return a fresh cached bundle identifier without calling native APIs."""
    with _input_source_snapshot_lock:
        bundle_id, refreshed_at = _input_source_snapshot
    age = clock() - refreshed_at
    if refreshed_at <= 0 or age < 0 or age > INPUT_SOURCE_CACHE_TTL_SECONDS:
        return ""
    return bundle_id


class IMECandidateReader:
    """Read candidates only for a registered input source anchored over the target."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        input_source_provider: Callable[[], str] | None = None,
        process_provider: Callable[[], Iterable[tuple[str, int]]] | None = None,
        ax_roots_provider: Callable[[int], Iterable[object]] | None = None,
        ax_rect_provider: Callable[[object], Rect | None] | None = None,
        window_provider: Callable[[], Iterable[WindowRecord]] | None = None,
        attribute_reader: Callable[[object, str], object] | None = None,
    ):
        self._clock = clock
        self._input_source_provider = (
            input_source_provider or cached_input_source_bundle_id
        )
        self._process_provider = process_provider or self._native_processes
        self._ax_roots_provider = ax_roots_provider or self._native_ax_roots
        self._ax_rect_provider = ax_rect_provider or self._native_ax_rect
        self._window_provider = window_provider or self._native_windows
        self._attribute_reader = attribute_reader or self._native_ax_attribute
        self.last_failure_reason: str | None = None

    def _fail(self, reason: str) -> None:
        self.last_failure_reason = reason
        return None

    def read(
        self,
        *,
        target_pid: int,
        target_bundle_id: str,
        profile_resolver: Callable[[str], ImeCandidateProfile | None] | None = None,
    ) -> CandidateSnapshot | None:
        self.last_failure_reason = None
        if target_pid <= 0 or not target_bundle_id:
            return self._fail("missing_target")
        resolve = profile_resolver or default_ime_profile_resolver
        try:
            input_source_bundle_id = self._input_source_provider()
            profile = resolve(input_source_bundle_id)
            if profile is None:
                return self._fail("input_source_mismatch")
            ime_pid = next(
                (
                    int(pid)
                    for bundle_id, pid in self._process_provider()
                    if bundle_id == profile.bundle_id and int(pid) > 0
                ),
                0,
            )
            if ime_pid <= 0:
                return self._fail("ime_process_unavailable")
            candidate_roots = []
            missing_candidate_bounds = False
            for root in self._ax_roots_provider(ime_pid):
                values = collect_static_text_values(
                    (root,),
                    self._attribute_reader,
                )
                if not values:
                    continue
                bounds = self._ax_rect_provider(root)
                if bounds is None:
                    missing_candidate_bounds = True
                    continue
                candidate_roots.append((values, bounds))
            if not candidate_roots:
                return self._fail(
                    "candidate_ax_bounds_unavailable"
                    if missing_candidate_bounds
                    else "candidate_ax_unavailable"
                )
            if len(candidate_roots) != 1:
                return self._fail("ambiguous_ax_windows")
            windows = tuple(self._window_provider())
            target_windows = tuple(
                window.bounds for window in windows if window.pid == target_pid
            )
            candidates, candidate_bounds = candidate_roots[0]
            if not any(
                candidate_is_in_composer(candidate_bounds, target)
                for target in target_windows
            ):
                return self._fail("candidate_outside_composer")
            return CandidateSnapshot(candidates, target_pid, self._clock())
        except Exception:
            return self._fail("native_capture_error")

    @staticmethod
    def _native_processes() -> Iterable[tuple[str, int]]:
        from AppKit import NSWorkspace

        workspace = NSWorkspace.sharedWorkspace()
        return tuple(
            (application.bundleIdentifier() or "", int(application.processIdentifier()))
            for application in workspace.runningApplications()
        )

    @staticmethod
    def _native_ax_attribute(element, attribute: str):
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

    def _native_ax_roots(self, pid: int) -> Iterable[object]:
        from ApplicationServices import (
            AXUIElementCreateApplication,
            AXUIElementSetMessagingTimeout,
        )

        application = AXUIElementCreateApplication(pid)
        AXUIElementSetMessagingTimeout(application, 0.1)
        windows = self._native_ax_attribute(application, "AXWindows")
        window_roots = object_sequence(windows)
        if window_roots:
            return window_roots
        return (application,)

    def _native_ax_rect(self, window) -> Rect | None:
        from ApplicationServices import (
            AXValueGetValue,
            kAXValueCGPointType,
            kAXValueCGSizeType,
        )

        position_value = self._native_ax_attribute(window, "AXPosition")
        size_value = self._native_ax_attribute(window, "AXSize")
        if position_value is None or size_value is None:
            return None
        position_result = AXValueGetValue(
            position_value,
            kAXValueCGPointType,
            None,
        )
        size_result = AXValueGetValue(
            size_value,
            kAXValueCGSizeType,
            None,
        )
        position = (
            position_result[1]
            if isinstance(position_result, tuple)
            and len(position_result) >= 2
            and position_result[0]
            else position_result
        )
        size = (
            size_result[1]
            if isinstance(size_result, tuple)
            and len(size_result) >= 2
            and size_result[0]
            else size_result
        )
        try:
            return Rect(
                x=float(position.x),
                y=float(position.y),
                width=float(size.width),
                height=float(size.height),
            )
        except (AttributeError, TypeError, ValueError):
            return None

    @staticmethod
    def _native_windows() -> Iterable[WindowRecord]:
        import Quartz

        options = (
            Quartz.kCGWindowListOptionOnScreenOnly
            | Quartz.kCGWindowListExcludeDesktopElements
        )
        window_info = Quartz.CGWindowListCopyWindowInfo(options, Quartz.kCGNullWindowID)
        records: list[WindowRecord] = []
        for item in window_info or ():
            pid = int(item.get(Quartz.kCGWindowOwnerPID, 0) or 0)
            bounds = item.get(Quartz.kCGWindowBounds) or {}
            rect = rect_from_window_bounds(bounds)
            if rect is None:
                continue
            if pid > 0 and rect.width > 0 and rect.height > 0:
                records.append(WindowRecord(pid=pid, bounds=rect))
        return tuple(records)


# Legacy alias: the Doubao-only reader generalised to the profile registry.
DoubaoCandidateReader = IMECandidateReader


class DoubaoCompositionState:
    """Pure, expiring state for one target application's Doubao composition."""

    def __init__(
        self,
        *,
        timeout_seconds: float,
        candidate_timeout_seconds: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.timeout_seconds = timeout_seconds
        self.candidate_timeout_seconds = (
            timeout_seconds
            if candidate_timeout_seconds is None
            else candidate_timeout_seconds
        )
        self._clock = clock
        self.clear()

    @property
    def has_active_candidate(self) -> bool:
        return bool(self._candidates)

    @property
    def selected_index(self) -> int:
        return self._selected_index

    @property
    def pending_preedit(self) -> str:
        return self._pending_preedit

    @property
    def confirmed_text(self) -> str:
        return self._confirmed_text

    def clear(self):
        self._target_pid = 0
        self._updated_at = 0.0
        self._candidates: tuple[str, ...] = ()
        self._selected_index = 0
        self._pending_preedit = ""
        self._confirmed_text = ""
        self._composer_trusted = False
        self._tainted = False

    def _prepare(self, target_pid: int) -> bool:
        if target_pid <= 0:
            self.clear()
            return False
        now = self._clock()
        expired = (
            self._updated_at > 0
            and now - self._updated_at > self.timeout_seconds
        )
        if expired or (self._target_pid > 0 and self._target_pid != target_pid):
            self.clear()
        if self._target_pid == 0:
            self._target_pid = target_pid
        self._updated_at = now
        return True

    def _append_confirmed(self, text: str):
        if not text:
            return
        available = MAX_COMPOSED_CHARS - len(self._confirmed_text)
        if available > 0:
            self._confirmed_text += text[:available]

    def record_printable(self, text: str, *, target_pid: int):
        if not self._prepare(target_pid):
            return
        printable = "".join(character for character in text if character.isprintable())
        if printable:
            available = MAX_COMPOSED_CHARS - len(self._pending_preedit)
            self._pending_preedit += printable[:available]

    def update_candidates(
        self,
        snapshot: CandidateSnapshot | None,
        *,
        target_pid: int,
    ):
        if not self._prepare(target_pid):
            return
        if snapshot is None:
            had_unconfirmed_input = bool(self._pending_preedit or self._candidates)
            self._candidates = ()
            self._selected_index = 0
            self._pending_preedit = ""
            if self._confirmed_text and had_unconfirmed_input:
                self._tainted = True
            return
        if snapshot.target_pid != target_pid:
            self.clear()
            return
        if self._clock() - snapshot.captured_at > self.candidate_timeout_seconds:
            self.clear()
            return
        candidates = tuple(
            dict.fromkeys(
                value.strip()
                for value in snapshot.candidates
                if isinstance(value, str) and value.strip()
            )
        )
        if not candidates:
            self.update_candidates(None, target_pid=target_pid)
            return
        if candidates != self._candidates:
            self._selected_index = 0
        self._candidates = candidates
        self._composer_trusted = True

    def _commit_candidate(self, index: int) -> CompositionResult:
        if not self._candidates:
            return CompositionResult()
        selected_index = min(max(index, 0), len(self._candidates) - 1)
        selected = self._candidates[selected_index]
        self._append_confirmed(selected)
        self._candidates = ()
        self._selected_index = 0
        self._pending_preedit = ""
        self._tainted = False
        return CompositionResult(candidate_committed=True, committed_text=selected)

    def handle_key(self, *, keycode: int, text: str, target_pid: int) -> CompositionResult:
        if not self._prepare(target_pid):
            return CompositionResult()
        if keycode == 51:  # Backspace
            if self._pending_preedit:
                self._pending_preedit = self._pending_preedit[:-1]
            elif self._confirmed_text:
                self._confirmed_text = self._confirmed_text[:-1]
            return CompositionResult()
        if keycode == 123 and self._candidates:  # Left
            self._selected_index = max(0, self._selected_index - 1)
            return CompositionResult()
        if keycode == 124 and self._candidates:  # Right
            self._selected_index = min(
                len(self._candidates) - 1,
                self._selected_index + 1,
            )
            return CompositionResult()
        if self._candidates and keycode in NUMBER_KEYCODE_TO_INDEX:
            return self._commit_candidate(NUMBER_KEYCODE_TO_INDEX[keycode])
        if self._candidates and keycode in (36, 49):  # Enter or Space
            return self._commit_candidate(self._selected_index)
        if not self._candidates and self._composer_trusted:
            if keycode == 49:
                self._append_confirmed(" ")
            elif keycode in NUMBER_KEYCODE_TO_INDEX:
                self._append_confirmed(text)
        return CompositionResult()

    def pop_submission(self, *, target_pid: int) -> str:
        if not self._prepare(target_pid) or self._candidates:
            return ""
        if self._tainted:
            self.clear()
            return ""
        content = self._confirmed_text
        self.clear()
        return content
