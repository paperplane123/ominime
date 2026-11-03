from ominime import context_capture
from ominime.context_capture import (
    AXFrame,
    CapturedContext,
    capture_accessibility_context,
    context_to_dict,
    is_text_entry_context,
    is_text_entry_role,
    is_secure_text_entry_context,
    read_ax_node,
)


from ominime.context_capture import select_container_node, frame_from_dict


def test_selects_nearest_large_parent_as_container():
    hierarchy = [
        {"role": "AXTextArea", "frame": {"x": 100, "y": 700, "width": 700, "height": 60}},
        {"role": "AXGroup", "frame": {"x": 80, "y": 100, "width": 760, "height": 660}},
        {"role": "AXWindow", "frame": {"x": 0, "y": 0, "width": 900, "height": 800}},
    ]
    assert select_container_node(hierarchy)["role"] == "AXGroup"


def test_select_container_falls_back_to_window():
    hierarchy = [
        {"role": "AXTextArea", "frame": {"x": 100, "y": 700, "width": 700, "height": 60}},
        {"role": "AXWindow", "frame": {"x": 0, "y": 0, "width": 900, "height": 800}},
    ]
    assert select_container_node(hierarchy)["role"] == "AXWindow"


def test_frame_from_dict_rejects_incomplete_frames():
    assert frame_from_dict({"x": 1, "y": 2}) is None


def test_text_entry_role_detection_accepts_text_controls():
    assert is_text_entry_role("AXTextArea")
    assert is_text_entry_role("AXTextField")
    assert is_text_entry_role("AXComboBox")
    assert is_text_entry_role("AXGroup", "AXSearchField")


def test_text_entry_context_rejects_non_input_roles():
    assert is_text_entry_context(CapturedContext(focused_role="AXTextArea"))
    assert not is_text_entry_context(CapturedContext(focused_role="AXGroup"))
    assert not is_text_entry_context(CapturedContext(focused_role="AXWebArea"))


def test_secure_text_entry_context_detects_secure_subrole_and_protected_content():
    assert is_secure_text_entry_context(
        CapturedContext(focused_role="AXTextField", focused_subrole="AXSecureTextField")
    )
    assert is_secure_text_entry_context(
        CapturedContext(focused_role="AXTextField", focused_protected=True)
    )
    assert not is_secure_text_entry_context(CapturedContext(focused_role="AXTextField"))


def test_serialized_context_never_contains_ax_values():
    context = CapturedContext(
        focused_role="AXTextArea",
        focused_value="private draft",
        hierarchy=[
            {"role": "AXTextArea", "value": "private draft"},
            {"role": "AXGroup", "value": "whole page"},
        ],
    )

    payload = context_to_dict(context)
    assert "focused_value" not in payload
    assert all("value" not in node for node in payload["hierarchy"])


def test_secure_ax_node_never_reads_value(monkeypatch):
    reads = []

    def fake_copy(_element, attribute):
        reads.append(attribute)
        return {
            "AXRole": "AXTextField",
            "AXSubrole": "AXSecureTextField",
            "AXProtectedContent": True,
        }.get(attribute)

    monkeypatch.setattr("ominime.context_capture.copy_ax_attribute", fake_copy)

    node = read_ax_node(object(), include_value=True)

    assert node["protected"] is True
    assert node["value"] is None
    assert "AXValue" not in reads


def test_capture_uses_target_process_focused_element(monkeypatch):
    focused = object()
    calls = []

    def fake_get_focused_element(target_pid=None):
        calls.append(target_pid)
        return focused

    monkeypatch.setattr(context_capture, "get_focused_element", fake_get_focused_element)
    monkeypatch.setattr(
        context_capture,
        "walk_ax_hierarchy",
        lambda element, max_depth: [{"role": "AXTextArea", "value": "hello"}],
    )

    result = capture_accessibility_context(target_pid=123)

    assert result.capture_status == "ok"
    assert result.focused_role == "AXTextArea"
    assert calls == [123]


def test_capture_does_not_fall_back_to_another_process_focus(monkeypatch):
    system_focused = object()
    calls = []

    def fake_get_focused_element(target_pid=None):
        calls.append(target_pid)
        return system_focused if target_pid is None else None

    monkeypatch.setattr(context_capture, "get_focused_element", fake_get_focused_element)
    monkeypatch.setattr(
        context_capture,
        "walk_ax_hierarchy",
        lambda element, max_depth: [{"role": "AXTextArea", "value": "hello"}],
    )

    result = capture_accessibility_context(target_pid=123)

    assert result.capture_status == "degraded"
    assert calls == [123]


def test_capture_reports_systemwide_and_process_focus_unavailable(monkeypatch):
    monkeypatch.setattr(context_capture, "get_focused_element", lambda target_pid=None: None)

    result = capture_accessibility_context(target_pid=123)

    assert result.capture_status == "degraded"
    assert result.capture_error == "focused element unavailable (pid 123)"


def test_focused_text_range_fallback_uses_only_verified_editable_field(monkeypatch):
    """A Safari text input may support AXStringForRange even without AXValue."""
    import sys
    from types import ModuleType

    focused = object()
    monkeypatch.setattr(context_capture, "get_focused_element", lambda pid: focused)
    attrs = {
        "AXRole": "AXTextField",
        "AXSubrole": None,
        "AXProtectedContent": False,
        "AXNumberOfCharacters": 6,
    }
    monkeypatch.setattr(
        context_capture, "copy_ax_attribute",
        lambda element, attr: attrs.get(attr),
    )
    calls = []
    ax = ModuleType("ApplicationServices")
    ax.kAXValueCFRangeType = 4
    ax.AXValueCreate = lambda typ, value: (typ, value)

    def copy_parameterized(element, name, value, _out):
        calls.append((element, name, value))
        return (0, "今天天气不错")

    ax.AXUIElementCopyParameterizedAttributeValue = copy_parameterized
    cf = ModuleType("CoreFoundation")
    cf.CFRange = lambda start, length: (start, length)
    monkeypatch.setitem(sys.modules, "ApplicationServices", ax)
    monkeypatch.setitem(sys.modules, "CoreFoundation", cf)

    content = context_capture.read_focused_text_via_range(123)
    assert content == "今天天气不错"
    assert calls == [(focused, "AXStringForRange", (4, (0, 6)))]


def test_focused_text_range_never_reads_password_or_page_body(monkeypatch):
    focused = object()
    monkeypatch.setattr(context_capture, "get_focused_element", lambda pid: focused)
    attrs = {
        "AXRole": "AXTextField",
        "AXSubrole": "AXSecureTextField",
        "AXProtectedContent": True,
        "AXNumberOfCharacters": 7,
    }
    reads = []
    monkeypatch.setattr(
        context_capture, "copy_ax_attribute",
        lambda element, attr: reads.append(attr) or attrs.get(attr),
    )

    assert context_capture.read_focused_text_via_range(123) == ""
    assert "AXNumberOfCharacters" not in reads
    attrs["AXRole"] = "AXWebArea"
    attrs["AXSubrole"] = None
    attrs["AXProtectedContent"] = False
    reads.clear()
    assert context_capture.read_focused_text_via_range(123) == ""
    assert "AXNumberOfCharacters" not in reads


def test_focused_text_range_rejects_large_input(monkeypatch):
    focused = object()
    monkeypatch.setattr(context_capture, "get_focused_element", lambda pid: focused)
    attrs = {
        "AXRole": "AXTextArea",
        "AXSubrole": None,
        "AXProtectedContent": False,
        "AXNumberOfCharacters": 4001,
    }
    monkeypatch.setattr(
        context_capture, "copy_ax_attribute",
        lambda element, attr: attrs.get(attr),
    )
    assert context_capture.read_focused_text_via_range(123, max_chars=4000) == ""
