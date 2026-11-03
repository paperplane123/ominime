"""probe_ime.py 的纯函数形状测试：只验证结构报告，不含候选文字，不触真机。"""

import importlib.util
import sys
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "src" / "ominime" / "scripts" / "probe_ime.py"


def _load_probe():
    spec = importlib.util.spec_from_file_location("probe_ime", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    # 先注册到 sys.modules，供 dataclass 解析类型注解使用。
    sys.modules["probe_ime"] = module
    spec.loader.exec_module(module)
    return module


class FakeNode:
    def __init__(self, role, value=None, children=None, position=None, size=None):
        self.role = role
        self.value = value
        self.children = children or []
        self.position = position
        self.size = size


def _attribute_reader(node, attribute):
    if attribute == "AXRole":
        return node.role
    if attribute == "AXValue":
        return node.value
    if attribute == "AXChildren":
        return node.children
    if attribute == "AXPosition":
        return node.position
    if attribute == "AXSize":
        return node.size
    return None


def _bounds_reader(node):
    if node.position is None or node.size is None:
        return None
    x, y = node.position
    w, h = node.size
    return (x, y, w, h)


def _candidate_window(texts, bounds=(100.0, 400.0, 200.0, 40.0)):
    children = [FakeNode("AXStaticText", value=text) for text in texts]
    return FakeNode(
        "AXWindow",
        children=children,
        position=(bounds[0], bounds[1]),
        size=(bounds[2], bounds[3]),
    )


def test_report_only_exposes_structure_and_no_candidate_text():
    probe = _load_probe()
    window = _candidate_window(["你好", "拟好", "泥好"])
    report = probe.build_report(
        active_input_source="com.example.ime",
        ime_bundle_id="com.example.ime",
        ime_pid=4242,
        windows=[window],
        read_attribute=_attribute_reader,
        read_bounds=_bounds_reader,
    )

    # 结构字段齐全，但绝不出现候选文字内容。
    assert report["windows"][0]["static_text_count"] == 3
    serialized = repr(report)
    for text in ("你好", "拟好", "泥好"):
        assert text not in serialized


def test_report_proven_when_single_anchorable_candidate_window():
    probe = _load_probe()
    window = _candidate_window(["a", "b"])
    report = probe.build_report(
        active_input_source="com.example.ime",
        ime_bundle_id="com.example.ime",
        ime_pid=4242,
        windows=[window],
        read_attribute=_attribute_reader,
        read_bounds=_bounds_reader,
        composer_bounds=(50.0, 200.0, 400.0, 300.0),
    )
    assert report["verdict"] == "PROVEN"
    assert report["features"]["single_candidate_window"] is True
    assert report["features"]["candidate_text_collectible"] is True
    assert report["features"]["geometry_readable"] is True
    assert report["features"]["anchored_in_composer"] is True


def test_report_not_proven_when_no_candidate_text():
    probe = _load_probe()
    empty_window = FakeNode("AXWindow", position=(0, 0), size=(10, 10))
    report = probe.build_report(
        active_input_source="com.example.ime",
        ime_bundle_id="com.example.ime",
        ime_pid=4242,
        windows=[empty_window],
        read_attribute=_attribute_reader,
        read_bounds=_bounds_reader,
    )
    assert report["verdict"] == "NOT_PROVEN:candidate_ax_unavailable"


def test_report_not_proven_when_process_missing():
    probe = _load_probe()
    report = probe.build_report(
        active_input_source="com.example.ime",
        ime_bundle_id="com.example.ime",
        ime_pid=0,
        windows=[],
        read_attribute=_attribute_reader,
        read_bounds=_bounds_reader,
    )
    assert report["verdict"] == "NOT_PROVEN:ime_process_unavailable"
    assert report["ime_process_found"] is False


def test_report_not_proven_when_multiple_candidate_windows():
    probe = _load_probe()
    windows = [_candidate_window(["a"]), _candidate_window(["b"])]
    report = probe.build_report(
        active_input_source="com.example.ime",
        ime_bundle_id="com.example.ime",
        ime_pid=4242,
        windows=windows,
        read_attribute=_attribute_reader,
        read_bounds=_bounds_reader,
    )
    assert report["verdict"] == "NOT_PROVEN:ambiguous_ax_windows"


def test_anchor_gate_rejects_candidate_far_below_composer():
    probe = _load_probe()
    assert (
        probe.candidate_anchored_in_composer(
            probe.CandidateGeometry(1000.0, 1000.0, 50.0, 10.0),
            (0.0, 0.0, 200.0, 200.0),
        )
        is False
    )


def _structure(static_text_count, bounds=(100.0, 400.0, 200.0, 40.0)):
    return probe_structure(static_text_count, bounds)


def probe_structure(static_text_count, bounds):
    probe = _load_probe()
    return probe.WindowStructure(
        index=0,
        static_text_count=static_text_count,
        has_position=bounds is not None,
        has_size=bounds is not None,
        bounds=bounds,
    )


def test_watch_keeps_peak_sample_after_candidate_disappears():
    """核心修复的性质：候选窗在采样间隙消失（用户去切终端）不影响结论。"""
    probe = _load_probe()
    scripts = [
        ([], False),  # 候选未出现
        ([_structure(3)], False),  # 候选窗出现，AX 可采
        ([], False),  # 用户切走，候选窗消失
        ([], False),
    ]
    calls = iter(scripts)

    def fake_provider(pid):
        return next(calls, ([], False))

    now = {"t": 0.0}

    def fake_clock():
        return now["t"]

    def fake_sleep(step):
        now["t"] += step

    samples, fallback_seen = probe.watch_samples(
        4242,
        fake_provider,
        duration_seconds=4.0,
        interval_seconds=1.0,
        sleep=fake_sleep,
        clock=fake_clock,
    )

    # 只有含候选文字的那次被保留，且报告仍判 PROVEN（锚定除外，未给几何时为待锚）。
    assert fallback_seen is False
    assert len(samples) == 1
    assert samples[0]["static_text_total"] == 3
    report = probe.build_watch_report(
        active_input_source="com.example.ime",
        ime_bundle_id="com.example.ime",
        ime_pid=4242,
        samples=samples,
    )
    assert report["verdict"] == "PROVEN_PENDING_ANCHOR"
    assert report["features"]["candidate_text_collectible"] is True
    assert report["features"]["observed_via"] == "ax"


def _cg_geometry_window():
    """CGWindowList 兜底窗：只有几何、文字计数恒为 0。"""
    return _load_probe().WindowStructure(
        index=0,
        static_text_count=0,
        has_position=True,
        has_size=True,
        bounds=(100.0, 400.0, 200.0, 40.0),
    )


def test_watch_not_proven_when_only_cgwindowlist_geometry_seen():
    """候选窗看得见但 AX 不暴露文字：正文不可采，判不接入。"""
    probe = _load_probe()
    now = {"t": 0.0}
    samples, fallback_seen = probe.watch_samples(
        4242,
        lambda pid: ([_cg_geometry_window()], True),
        duration_seconds=1.0,
        interval_seconds=0.5,
        sleep=lambda step: now.__setitem__("t", now["t"] + step),
        clock=lambda: now["t"],
    )
    assert samples == []
    assert fallback_seen is True
    report = probe.build_watch_report(
        active_input_source="com.example.ime",
        ime_bundle_id="com.example.ime",
        ime_pid=4242,
        samples=samples,
        fallback_seen=fallback_seen,
    )
    assert report["verdict"] == (
        "NOT_PROVEN:candidate_window_visible_but_no_ax_static_text"
    )
    assert report["features"]["candidate_text_collectible"] is False


def test_watch_report_exposes_no_candidate_text():
    probe = _load_probe()
    now = {"t": 0.0}
    samples, _fallback = probe.watch_samples(
        4242,
        lambda pid: ([_structure(5)], False),
        duration_seconds=1.0,
        interval_seconds=0.5,
        sleep=lambda step: now.__setitem__("t", now["t"] + step),
        clock=lambda: now["t"],
    )
    report = probe.build_watch_report(
        active_input_source="com.example.ime",
        ime_bundle_id="com.example.ime",
        ime_pid=4242,
        samples=samples,
    )
    serialized = repr(report) + repr(samples)
    # provider 从未返回过文字，报告中只允许出现计数与几何。
    assert "static_text_count" in serialized
    assert "你好" not in serialized
