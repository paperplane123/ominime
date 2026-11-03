#!/usr/bin/env python3
"""开发态输入法候选窗可行性探测（Part B / B1，先决人工检查点）。

用途：在真机上确认「当前激活的输入法候选窗」是否暴露可被现有采集链路复用的
AX 结构，从而判断能否把它接入 `IMECandidateReader` 的多输入法注册表。

设计约束（与 `ime_candidate_capture.py` 的隐私底线一致）：
- 只输出**结构特征**（窗口数量、AXStaticText 计数、几何是否可读、锚定判定），
  **绝不输出候选文字内容**。
- 原生框架（Carbon / ApplicationServices / AppKit）只在 `main()` 内部惰性导入，
  纯函数 `build_report` 走依赖注入，可在无 macOS 依赖下用合成 fixture 测试。

典型用法（无需手速同步，候选窗消失也不影响）::

    # 1) 先跑到输入法面板把微信输入法切成当前激活输入源
    ./venv/bin/python src/ominime/scripts/probe_ime.py --list-sources

    # 2) 终端里先启动后台采样，然后切回任意 app 正常打字打出候选：
    ./venv/bin/python src/ominime/scripts/probe_ime.py --watch 30
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence


@dataclass(frozen=True)
class WindowStructure:
    """单个候选窗的结构摘要（不含任何候选文字内容）。"""

    index: int
    static_text_count: int
    has_position: bool
    has_size: bool
    bounds: tuple[float, float, float, float] | None


@dataclass(frozen=True)
class CandidateGeometry:
    """候选窗矩形（x, y, width, height）。"""

    x: float
    y: float
    width: float
    height: float


def summarize_window(
    index: int,
    window: object,
    read_attribute: Callable[[object, str], object],
    read_bounds: Callable[[object], tuple[float, float, float, float] | None],
    *,
    max_nodes: int = 200,
) -> WindowStructure:
    """遍历一个窗口的 AX 子树，只统计结构与几何，不采集候选文字。"""
    pending: list[object] = [window]
    visited: set[int] = set()
    static_text_count = 0
    processed = 0
    while pending and processed < max_nodes:
        node = pending.pop(0)
        identity = id(node)
        if identity in visited:
            continue
        visited.add(identity)
        processed += 1
        if read_attribute(node, "AXRole") == "AXStaticText":
            value = read_attribute(node, "AXValue")
            if isinstance(value, str) and value.strip():
                static_text_count += 1
        children = read_attribute(node, "AXChildren")
        if isinstance(children, (list, tuple)):
            pending.extend(children)
    bounds = read_bounds(window)
    return WindowStructure(
        index=index,
        static_text_count=static_text_count,
        has_position=read_attribute(window, "AXPosition") is not None,
        has_size=read_attribute(window, "AXSize") is not None,
        bounds=bounds,
    )


def candidate_anchored_in_composer(
    candidate: CandidateGeometry,
    composer: tuple[float, float, float, float],
    *,
    composer_top_fraction: float = 0.55,
) -> bool:
    """候选窗是否落在目标输入区（复用 reader 的锚定几何直觉，仅结构判定）。"""
    cx, cy, cw, ch = composer
    if min(candidate.width, candidate.height, cw, ch) <= 0:
        return False
    if candidate.width > cw or candidate.height > ch * 0.25:
        return False
    center_x = candidate.x + candidate.width / 2
    center_y = candidate.y + candidate.height / 2
    return (
        cx <= center_x <= cx + cw
        and cy + ch * composer_top_fraction <= center_y <= cy + ch
    )


def build_report(
    *,
    active_input_source: str,
    ime_bundle_id: str,
    ime_pid: int,
    windows: Sequence[object],
    read_attribute: Callable[[object, str], object],
    read_bounds: Callable[[object], tuple[float, float, float, float] | None],
    composer_bounds: tuple[float, float, float, float] | None = None,
) -> dict:
    """把候选窗列表折算成结构化可行性报告（纯数据，无候选文字）。"""
    structures = [
        summarize_window(i, window, read_attribute, read_bounds)
        for i, window in enumerate(windows)
    ]
    collectible = any(s.static_text_count > 0 for s in structures)
    geometry_readable = all(
        s.has_position and s.has_size and s.bounds is not None for s in structures
    )
    single_candidate_window = len(structures) == 1

    anchored: bool | None = None
    if composer_bounds is not None and structures and structures[0].bounds:
        bx, by, bw, bh = structures[0].bounds
        anchored = candidate_anchored_in_composer(
            CandidateGeometry(bx, by, bw, bh),
            composer_bounds,
        )

    verdict = _derive_verdict(
        ime_pid=ime_pid,
        structures=structures,
        collectible=collectible,
        geometry_readable=geometry_readable,
        single_candidate_window=single_candidate_window,
        anchored=anchored,
    )

    return {
        "active_input_source": active_input_source,
        "ime_bundle_id": ime_bundle_id,
        "ime_process_found": ime_pid > 0,
        "ime_pid": ime_pid,
        "ax_window_count": len(structures),
        "windows": [
            {
                "index": s.index,
                "static_text_count": s.static_text_count,
                "has_position": s.has_position,
                "has_size": s.has_size,
                "bounds": list(s.bounds) if s.bounds else None,
            }
            for s in structures
        ],
        "features": {
            "single_candidate_window": single_candidate_window,
            "candidate_text_collectible": collectible,
            "geometry_readable": geometry_readable,
            "anchored_in_composer": anchored,
        },
        "verdict": verdict,
    }


def _derive_verdict(
    *,
    ime_pid: int,
    structures: Sequence[WindowStructure],
    collectible: bool,
    geometry_readable: bool,
    single_candidate_window: bool,
    anchored: bool | None,
) -> str:
    if ime_pid <= 0:
        return "NOT_PROVEN:ime_process_unavailable"
    if not structures:
        return "NOT_PROVEN:no_ax_windows"
    if not collectible:
        return "NOT_PROVEN:candidate_ax_unavailable"
    if not geometry_readable:
        return "NOT_PROVEN:candidate_ax_bounds_unavailable"
    if not single_candidate_window:
        return "NOT_PROVEN:ambiguous_ax_windows"
    if anchored is False:
        return "NOT_PROVEN:candidate_outside_composer"
    if anchored is None:
        return "PROVEN_PENDING_ANCHOR"
    return "PROVEN"


# --------------------------------------------------------------------------- #
# 原生实现（惰性导入，仅在真机 main() 路径使用）
# --------------------------------------------------------------------------- #


def _cfstring_to_py(core_foundation, value) -> str:
    if not value:
        return ""
    import ctypes

    # 内存安全门：TISGetInputSourceProperty 对某些输入源可能返回非 CFString
    # 指针，直接按 CFString 调用 CFStringGetLength 会越界崩溃（间歇性 segfault）。
    try:
        if core_foundation.CFGetTypeID(value) != core_foundation.CFStringGetTypeID():
            return ""
    except Exception:
        return ""
    encoding_utf8 = 0x08000100
    length = core_foundation.CFStringGetLength(value)
    if length < 0 or length > 4096:
        return ""
    max_size = core_foundation.CFStringGetMaximumSizeForEncoding(
        length, encoding_utf8
    )
    if max_size < 0 or max_size > 65536:
        return ""
    size = max_size + 1
    buffer = ctypes.create_string_buffer(size)
    if not core_foundation.CFStringGetCString(value, buffer, size, encoding_utf8):
        return ""
    return buffer.value.decode("utf-8", "replace")


def list_keyboard_input_sources() -> list[dict]:
    """枚举本机全部键盘输入源的 bundle id 与本地化名（仅元数据）。"""
    import ctypes

    hitoolbox = ctypes.CDLL(
        "/System/Library/Frameworks/Carbon.framework/Frameworks/"
        "HIToolbox.framework/HIToolbox"
    )
    core_foundation = ctypes.CDLL(
        "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
    )
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
    core_foundation.CFGetTypeID.argtypes = [ctypes.c_void_p]
    core_foundation.CFGetTypeID.restype = ctypes.c_ulong
    core_foundation.CFStringGetTypeID.restype = ctypes.c_ulong
    core_foundation.CFRelease.argtypes = [ctypes.c_void_p]
    core_foundation.CFArrayGetCount.argtypes = [ctypes.c_void_p]
    core_foundation.CFArrayGetCount.restype = ctypes.c_long
    core_foundation.CFArrayGetValueAtIndex.argtypes = [ctypes.c_void_p, ctypes.c_long]
    core_foundation.CFArrayGetValueAtIndex.restype = ctypes.c_void_p

    hitoolbox.TISCreateInputSourceList.argtypes = [ctypes.c_void_p, ctypes.c_bool]
    hitoolbox.TISCreateInputSourceList.restype = ctypes.c_void_p
    hitoolbox.TISGetInputSourceProperty.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    hitoolbox.TISGetInputSourceProperty.restype = ctypes.c_void_p

    bundle_key = ctypes.c_void_p.in_dll(hitoolbox, "kTISPropertyBundleID").value
    name_key = ctypes.c_void_p.in_dll(hitoolbox, "kTISPropertyLocalizedName").value
    category_key = ctypes.c_void_p.in_dll(
        hitoolbox, "kTISPropertyInputSourceCategory"
    ).value
    mode_key = ctypes.c_void_p.in_dll(hitoolbox, "kTISPropertyInputSourceType").value

    # includeAllInterfaces=True 才能列出输入模式（输入法候选归属的 mode）。
    source_list = hitoolbox.TISCreateInputSourceList(None, True)
    results: list[dict] = []
    if not source_list:
        return results
    try:
        count = core_foundation.CFArrayGetCount(source_list)
        for i in range(count):
            source = core_foundation.CFArrayGetValueAtIndex(source_list, i)
            if not source:
                continue
            bundle_id = _cfstring_to_py(
                core_foundation,
                hitoolbox.TISGetInputSourceProperty(source, bundle_key),
            )
            if not bundle_id:
                continue
            name = _cfstring_to_py(
                core_foundation,
                hitoolbox.TISGetInputSourceProperty(source, name_key),
            )
            category = _cfstring_to_py(
                core_foundation,
                hitoolbox.TISGetInputSourceProperty(source, category_key),
            )
            source_type = _cfstring_to_py(
                core_foundation,
                hitoolbox.TISGetInputSourceProperty(source, mode_key),
            )
            results.append(
                {
                    "bundle_id": bundle_id,
                    "name": name,
                    "category": category,
                    "type": source_type,
                }
            )
    finally:
        core_foundation.CFRelease(source_list)
    deduped: dict[str, dict] = {}
    for item in results:
        deduped.setdefault(item["bundle_id"], item)
    return sorted(deduped.values(), key=lambda item: item["bundle_id"])


def _find_ime_pid(bundle_id: str) -> int:
    from AppKit import NSWorkspace

    workspace = NSWorkspace.sharedWorkspace()
    for application in workspace.runningApplications():
        if (application.bundleIdentifier() or "") == bundle_id:
            pid = int(application.processIdentifier())
            if pid > 0:
                return pid
    return 0


def _read_main_thread_bundle_id() -> str:
    # 复用 reader 的实现（要求主线程）。
    from ominime.ime_candidate_capture import current_input_source_bundle_id

    return current_input_source_bundle_id()


def _native_readers(pid: int):
    from ApplicationServices import (
        AXUIElementCopyAttributeValue,
        AXUIElementCreateApplication,
        AXUIElementSetMessagingTimeout,
        AXValueGetValue,
        kAXValueCGPointType,
        kAXValueCGSizeType,
    )

    def read_attribute(element, attribute):
        try:
            result = AXUIElementCopyAttributeValue(element, attribute, None)
        except TypeError:
            result = AXUIElementCopyAttributeValue(element, attribute)
        if isinstance(result, tuple):
            if len(result) >= 2 and result[0] == 0:
                return result[1]
            return None
        return result

    def read_bounds(window):
        position_value = read_attribute(window, "AXPosition")
        size_value = read_attribute(window, "AXSize")
        if position_value is None or size_value is None:
            return None
        pos = AXValueGetValue(position_value, kAXValueCGPointType, None)
        size = AXValueGetValue(size_value, kAXValueCGSizeType, None)
        point = pos[1] if isinstance(pos, tuple) and pos[0] else pos
        dim = size[1] if isinstance(size, tuple) and size[0] else size
        try:
            x = float(point.x if hasattr(point, "x") else point[0])
            y = float(point.y if hasattr(point, "y") else point[1])
            w = float(dim.width if hasattr(dim, "width") else dim[0])
            h = float(dim.height if hasattr(dim, "height") else dim[1])
        except (TypeError, ValueError, AttributeError):
            return None
        return (x, y, w, h)

    app = AXUIElementCreateApplication(pid)
    AXUIElementSetMessagingTimeout(app, 0.2)
    windows = read_attribute(app, "AXWindows")
    window_list = list(windows) if isinstance(windows, (list, tuple)) else []
    return window_list, read_attribute, read_bounds


def _snapshot_structures(
    pid: int,
    read_attribute: Callable[[object, str], object],
    read_bounds: Callable[[object], tuple[float, float, float, float] | None],
) -> tuple[list[WindowStructure], bool]:
    """一次采样：AX 窗口优先；AX 不暴露时用 CGWindowList 兜底（仅几何结构）。

    返回 (结构列表, 是否来自 CGWindowList 兜底)。"""
    from ApplicationServices import (
        AXUIElementCreateApplication,
        AXUIElementSetMessagingTimeout,
    )

    app = AXUIElementCreateApplication(pid)
    AXUIElementSetMessagingTimeout(app, 0.2)
    windows = read_attribute(app, "AXWindows")
    window_list = list(windows) if isinstance(windows, (list, tuple)) else []
    if window_list:
        return (
            [
                summarize_window(i, window, read_attribute, read_bounds)
                for i, window in enumerate(window_list)
            ],
            False,
        )
    return (_cgwindow_structures(pid), True)


def _cgwindow_structures(pid: int) -> list[WindowStructure]:
    """IME 候选窗常是专用面板窗、不在 AXWindows 里，用 CGWindowList 兜底枚举几何。"""
    from Quartz import CGWindowListCopyWindowInfo, kCGWindowListOptionAll

    try:
        infos = CGWindowListCopyWindowInfo(kCGWindowListOptionAll, 0) or ()
    except Exception:
        return []
    structures: list[WindowStructure] = []
    for info in infos:
        try:
            if int(info.get("kCGWindowOwnerPID", -1)) != pid:
                continue
            bounds = info.get("kCGWindowBounds")
            if bounds is None:
                continue
            rect = (
                float(bounds.get("X", 0.0)),
                float(bounds.get("Y", 0.0)),
                float(bounds.get("Width", 0.0)),
                float(bounds.get("Height", 0.0)),
            )
        except (AttributeError, TypeError, ValueError):
            continue
        structures.append(
            WindowStructure(
                index=len(structures),
                static_text_count=0,
                has_position=True,
                has_size=True,
                bounds=rect,
            )
        )
    return structures


def watch_samples(
    pid: int,
    sample_provider: Callable[[int], tuple[list[WindowStructure], bool]],
    *,
    duration_seconds: float = 30.0,
    interval_seconds: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[list[dict], bool]:
    """后台连续采样：只要任意一次看到候选文字结构，就永久保留为峰值。

    打字期间候选窗出现又消失都没关系——峰值样本会被记录下来。"""
    deadline = clock() + duration_seconds
    samples: list[dict] = []
    peak = -1
    fallback_seen = False
    while clock() < deadline:
        try:
            structures, from_fallback = sample_provider(pid)
        except Exception:
            structures, from_fallback = [], False
        total_texts = sum(s.static_text_count for s in structures)
        if from_fallback and structures:
            # 兜底窗只有几何（文字计数恒为 0）：单独记录“看得见但正文不可采”。
            fallback_seen = True
        if total_texts > 0 and total_texts >= peak:
            peak = total_texts
            samples.append(
                {
                    "at": round(clock(), 3),
                    "source": "cgwindowlist" if from_fallback else "ax",
                    "window_count": len(structures),
                    "static_text_total": total_texts,
                    "windows": [
                        {
                            "index": s.index,
                            "static_text_count": s.static_text_count,
                            "has_position": s.has_position,
                            "has_size": s.has_size,
                            "bounds": list(s.bounds) if s.bounds else None,
                        }
                        for s in structures
                    ],
                }
            )
        sleep(interval_seconds)
    return samples, fallback_seen


def build_watch_report(
    *,
    active_input_source: str,
    ime_bundle_id: str,
    ime_pid: int,
    samples: Sequence[dict],
    fallback_seen: bool = False,
) -> dict:
    """把峰值采样折算成可行性报告（与 build_report 同形，同样只含结构不含文字）。"""
    best = None
    for sample in samples:
        if best is None or sample["static_text_total"] > best["static_text_total"]:
            best = sample
    any_from_ax = any(sample.get("source") == "ax" for sample in samples)
    any_from_fallback = any(
        sample.get("source") == "cgwindowlist" for sample in samples
    )
    structures = (
        [
            WindowStructure(
                index=w["index"],
                static_text_count=w["static_text_count"],
                has_position=w["has_position"],
                has_size=w["has_size"],
                bounds=tuple(w["bounds"]) if w["bounds"] else None,
            )
            for w in best["windows"]
        ]
        if best
        else []
    )
    collectible = best is not None and any_from_ax
    geometry_readable = bool(structures) and all(
        s.has_position and s.has_size and s.bounds is not None for s in structures
    )
    single_candidate_window = bool(structures) and len(structures) == 1
    verdict = _derive_verdict(
        ime_pid=ime_pid,
        structures=structures,
        collectible=collectible,
        geometry_readable=geometry_readable,
        single_candidate_window=single_candidate_window,
        anchored=None,
    )
    if best is None:
        if fallback_seen:
            verdict = "NOT_PROVEN:candidate_window_visible_but_no_ax_static_text"
        else:
            verdict = "NOT_PROVEN:no_candidate_window_observed"
    return {
        "active_input_source": active_input_source,
        "ime_bundle_id": ime_bundle_id,
        "ime_process_found": ime_pid > 0,
        "ime_pid": ime_pid,
        "ax_window_count": best["window_count"] if best else 0,
        "windows": [
            {
                "index": s.index,
                "static_text_count": s.static_text_count,
                "has_position": s.has_position,
                "has_size": s.has_size,
                "bounds": list(s.bounds) if s.bounds else None,
            }
            for s in structures
        ],
        "features": {
            "single_candidate_window": single_candidate_window,
            "candidate_text_collectible": collectible,
            "geometry_readable": geometry_readable,
            "anchored_in_composer": None,
            "observed_via": (
                "ax"
                if any_from_ax
                else ("cgwindowlist" if (any_from_fallback or fallback_seen) else None)
            ),
        },
        "verdict": verdict,
    }


def _print_report(report: dict) -> None:
    import json

    print(json.dumps(report, ensure_ascii=False, indent=2))


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bundle-id",
        default="",
        help="要探测的输入法 bundle id；缺省时使用当前激活输入源。",
    )
    parser.add_argument(
        "--list-sources",
        action="store_true",
        help="只列出本机全部键盘输入源的 bundle id 与名称后退出。",
    )
    parser.add_argument(
        "--watch",
        type=float,
        default=0.0,
        metavar="SECONDS",
        help=(
            "后台采样时长（秒）。先启动本命令，再去任意 app 正常打字打出候选；"
            "候选窗消失不影响结论，只要任意一次采样看到结构就记为峰值。"
        ),
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        sources = list_keyboard_input_sources()
    except Exception as exc:  # pragma: no cover - 原生环境相关
        print(f"无法枚举输入源（需在 macOS 上运行）：{exc}", file=sys.stderr)
        return 2

    print("本机键盘输入源（bundle id  ->  名称）：")
    for item in sources:
        print(f"  {item['bundle_id']}  ->  {item['name']}")

    try:
        active = _read_main_thread_bundle_id()
    except Exception as exc:  # pragma: no cover - 原生环境相关
        print(f"当前激活输入源读取失败：{exc}", file=sys.stderr)
        active = ""
    print(f"\n当前激活输入源 bundle id: {active or '(未知，请确认在输入法面板已激活)'}")

    if args.list_sources:
        return 0

    target = args.bundle_id or active
    if not target:
        print(
            "\n未指定 --bundle-id 且当前无激活输入源，无法探测候选窗结构。",
            file=sys.stderr,
        )
        return 2

    pid = _find_ime_pid(target)
    if pid <= 0:
        print(
            f"\n未找到输入法进程（bundle id: {target}）。"
            "请确认该输入法已安装并正在运行。",
            file=sys.stderr,
        )
        return 3

    window_list, read_attribute, read_bounds = _native_readers(pid)

    if args.watch > 0:
        print(
            f"\n后台采样 {args.watch:.0f} 秒：现在请切到你的 app，"
            "用该输入法正常打字打出候选窗（无需掐表，出现又消失都会被记录）。"
        )
        samples, fallback_seen = watch_samples(
            pid,
            lambda target_pid: _snapshot_structures(
                target_pid, read_attribute, read_bounds
            ),
            duration_seconds=args.watch,
        )
        report = build_watch_report(
            active_input_source=active,
            ime_bundle_id=target,
            ime_pid=pid,
            samples=samples,
            fallback_seen=fallback_seen,
        )
        print(f"\n共捕捉到 {len(samples)} 次含候选文字的采样（峰值样本详情如下）：")
        _print_report(report)
        print(
            "\n请把以上结论如实记入 docs/verification/wxime-candidate-feasibility.md。"
            " verdict=PROVEN 才可把该输入法加入 IME_CANDIDATE_PROFILES 注册表；"
            "若 observed_via=cgwindowlist 且 AX 未暴露文字，则候选正文不可采，维持不接入。",
        )
        return 0

    report = build_report(
        active_input_source=active,
        ime_bundle_id=target,
        ime_pid=pid,
        windows=window_list,
        read_attribute=read_attribute,
        read_bounds=read_bounds,
    )
    print("\n候选窗结构可行性报告（仅结构，不含候选文字）：")
    _print_report(report)
    print(
        "\n请把以上结论如实记入 docs/verification/wxime-candidate-feasibility.md。"
        " verdict=PROVEN 才可把该输入法加入 IME_CANDIDATE_PROFILES 注册表。",
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - 入口
    raise SystemExit(main())
