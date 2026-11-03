"""Accessibility context capture primitives."""

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class AXFrame:
    x: float
    y: float
    width: float
    height: float

    def to_dict(self) -> dict[str, float]:
        return {
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
        }


@dataclass
class CapturedContext:
    focused_frame: AXFrame | None = None
    container_frame: AXFrame | None = None
    window_frame: AXFrame | None = None
    focused_role: str | None = None
    focused_subrole: str | None = None
    focused_title: str | None = None
    focused_description: str | None = None
    focused_identifier: str | None = None
    focused_protected: bool = False
    focused_value: str | None = field(default=None, repr=False)
    container_role: str | None = None
    container_title: str | None = None
    window_title: str | None = None
    hierarchy: list[dict] = field(default_factory=list)
    capture_status: str = "ok"
    capture_error: str | None = None


TEXT_ENTRY_ROLES = {
    "AXComboBox",
    "AXTextArea",
    "AXTextField",
}

TEXT_ENTRY_SUBROLES = {
    "AXSearchField",
}

SECURE_TEXT_ENTRY_SUBROLES = {
    "AXSecureTextField",
}


def is_text_entry_context(context: CapturedContext | dict | None) -> bool:
    """Return whether the focused AX element is a text-entry control."""
    if context is None:
        return False

    if isinstance(context, dict):
        role = context.get("focused_role")
        subrole = context.get("focused_subrole")
    else:
        role = getattr(context, "focused_role", None)
        subrole = getattr(context, "focused_subrole", None)

    return is_text_entry_role(role, subrole)


def is_text_entry_role(role: str | None, subrole: str | None = None) -> bool:
    if role in TEXT_ENTRY_ROLES:
        return True
    return subrole in TEXT_ENTRY_SUBROLES


def is_secure_text_entry_context(context: CapturedContext | dict | None) -> bool:
    """Return whether the focused control can contain protected credentials."""
    if context is None:
        return False
    if isinstance(context, dict):
        subrole = context.get("focused_subrole")
        protected = context.get("focused_protected")
    else:
        subrole = getattr(context, "focused_subrole", None)
        protected = getattr(context, "focused_protected", False)
    return subrole in SECURE_TEXT_ENTRY_SUBROLES or protected is True


def focused_field_identity(context: CapturedContext | dict | None) -> str | None:
    """Build a stable-enough identity for isolating focused-field caches."""
    if context is None:
        return None
    if isinstance(context, dict):
        identifier = context.get("focused_identifier")
        window_title = context.get("window_title")
        frame = context.get("focused_frame")
    else:
        identifier = getattr(context, "focused_identifier", None)
        window_title = getattr(context, "window_title", None)
        raw_frame = getattr(context, "focused_frame", None)
        frame = raw_frame.to_dict() if isinstance(raw_frame, AXFrame) else raw_frame

    if identifier:
        return f"id:{window_title or ''}:{identifier}"
    if isinstance(frame, dict) and all(key in frame for key in ("x", "y", "width", "height")):
        values = tuple(round(float(frame[key]), 1) for key in ("x", "y", "width", "height"))
        return f"frame:{window_title or ''}:{values[0]}:{values[1]}:{values[2]}:{values[3]}"
    return None


def context_to_dict(context: CapturedContext) -> dict[str, Any]:
    """Serialize captured context for event metadata and JSON storage."""
    return {
        "focused_frame": context.focused_frame.to_dict() if context.focused_frame else None,
        "container_frame": context.container_frame.to_dict() if context.container_frame else None,
        "window_frame": context.window_frame.to_dict() if context.window_frame else None,
        "focused_role": context.focused_role,
        "focused_subrole": context.focused_subrole,
        "focused_title": context.focused_title,
        "focused_description": context.focused_description,
        "focused_identifier": context.focused_identifier,
        "focused_protected": context.focused_protected,
        "container_role": context.container_role,
        "container_title": context.container_title,
        "window_title": context.window_title,
        "hierarchy": [
            {key: value for key, value in node.items() if key != "value"}
            for node in context.hierarchy
        ],
        "capture_status": context.capture_status,
        "capture_error": context.capture_error,
    }


def frame_from_dict(value: dict | None) -> AXFrame | None:
    if not isinstance(value, dict):
        return None
    required = ("x", "y", "width", "height")
    if not all(key in value for key in required):
        return None
    return AXFrame(
        float(value["x"]),
        float(value["y"]),
        float(value["width"]),
        float(value["height"]),
    )


def select_container_node(hierarchy: list[dict]) -> dict | None:
    """Select the nearest useful dialog/container ancestor."""
    if not hierarchy:
        return None

    focused_frame = frame_from_dict(hierarchy[0].get("frame"))
    window_node = None
    container_roles = {"AXGroup", "AXScrollArea", "AXSplitGroup"}

    for node in hierarchy[1:]:
        role = node.get("role")
        if role == "AXWindow":
            window_node = node
            continue
        if role not in container_roles:
            continue
        frame = frame_from_dict(node.get("frame"))
        if frame is None:
            continue
        if focused_frame is None or _is_useful_container_frame(focused_frame, frame):
            return node

    return window_node


def _is_useful_container_frame(focused: AXFrame, candidate: AXFrame) -> bool:
    if candidate.width < focused.width or candidate.height < focused.height:
        return False
    return candidate.height >= max(160, focused.height * 3)


def capture_accessibility_context(
    max_depth: int = 12,
    target_pid: int | None = None,
) -> CapturedContext:
    """Capture focused element metadata through macOS Accessibility APIs."""
    try:
        has_target_pid = target_pid is not None and target_pid > 0
        focused = get_focused_element(target_pid if has_target_pid else None)
        if focused is None:
            error = "focused element unavailable"
            if has_target_pid:
                error = f"{error} (pid {target_pid})"
            return CapturedContext(capture_status="degraded", capture_error=error)

        hierarchy = walk_ax_hierarchy(focused, max_depth=max_depth)
        focused_node = hierarchy[0] if hierarchy else {}
        container_node = select_container_node(hierarchy)
        window_node = next((node for node in hierarchy if node.get("role") == "AXWindow"), None)

        focused_frame = frame_from_dict(focused_node.get("frame"))
        container_frame = frame_from_dict(container_node.get("frame")) if container_node else None
        window_frame = frame_from_dict(window_node.get("frame")) if window_node else None

        return CapturedContext(
            focused_frame=focused_frame,
            container_frame=container_frame,
            window_frame=window_frame,
            focused_role=focused_node.get("role"),
            focused_subrole=focused_node.get("subrole"),
            focused_title=focused_node.get("title"),
            focused_description=focused_node.get("description"),
            focused_identifier=focused_node.get("identifier"),
            focused_protected=focused_node.get("protected") is True,
            focused_value=focused_node.get("value"),
            container_role=container_node.get("role") if container_node else None,
            container_title=container_node.get("title") if container_node else None,
            window_title=window_node.get("title") if window_node else None,
            hierarchy=hierarchy,
        )
    except Exception as exc:
        return CapturedContext(capture_status="degraded", capture_error=str(exc))


def get_focused_element(target_pid: int | None = None):
    try:
        from ApplicationServices import AXUIElementCreateApplication, AXUIElementCreateSystemWide
    except Exception:
        return None

    root = (
        AXUIElementCreateApplication(target_pid)
        if target_pid is not None and target_pid > 0
        else AXUIElementCreateSystemWide()
    )
    return copy_ax_attribute(root, "AXFocusedUIElement")


def read_focused_text_via_range(target_pid: int, max_chars: int = 4000) -> str:
    """Read only a verified, non-secure focused text control via AXStringForRange.

    This is an optional macOS fallback when AXValue is empty (not a general
    webpage or accessibility-tree text reader). A provider may not implement
    the parameterized attribute; all unsupported cases fail closed.
    """
    if target_pid <= 0 or max_chars <= 0:
        return ""
    focused = get_focused_element(target_pid)
    if focused is None:
        return ""
    role = _string_or_none(copy_ax_attribute(focused, "AXRole"))
    subrole = _string_or_none(copy_ax_attribute(focused, "AXSubrole"))
    if not is_text_entry_role(role, subrole):
        return ""
    if (
        subrole in SECURE_TEXT_ENTRY_SUBROLES
        or bool(copy_ax_attribute(focused, "AXProtectedContent"))
    ):
        return ""

    count = copy_ax_attribute(focused, "AXNumberOfCharacters")
    # AX character ranges are UTF-16-based. Never read a whole document.
    if isinstance(count, bool) or not isinstance(count, (int, float)):
        return ""
    count = int(count)
    if count <= 0 or count > max_chars:
        return ""

    try:
        from ApplicationServices import (
            AXUIElementCopyParameterizedAttributeValue, AXValueCreate,
            kAXValueCFRangeType,
        )
        from CoreFoundation import CFRange

        value = AXValueCreate(kAXValueCFRangeType, CFRange(0, count))
        if value is None:
            return ""
        result = AXUIElementCopyParameterizedAttributeValue(
            focused, "AXStringForRange", value, None
        )
        if not isinstance(result, tuple) or len(result) < 2 or result[0] != 0:
            return ""
        text = result[1]
        if not isinstance(text, str) or len(text) > max_chars:
            return ""
        return text
    except Exception:
        return ""


def walk_ax_hierarchy(element, max_depth: int = 12) -> list[dict]:
    hierarchy = []
    current = element
    seen = set()

    for _ in range(max_depth):
        if current is None:
            break
        marker = id(current)
        if marker in seen:
            break
        seen.add(marker)

        node = read_ax_node(current, include_value=not hierarchy)
        hierarchy.append(node)
        current = copy_ax_attribute(current, "AXParent")

    return hierarchy


def read_ax_node(element, *, include_value: bool = True) -> dict[str, Any]:
    frame = copy_ax_attribute(element, "AXFrame")
    role = _string_or_none(copy_ax_attribute(element, "AXRole"))
    subrole = _string_or_none(copy_ax_attribute(element, "AXSubrole"))
    protected = copy_ax_attribute(element, "AXProtectedContent")
    is_secure = subrole in SECURE_TEXT_ENTRY_SUBROLES or bool(protected)
    node = {
        "role": role,
        "subrole": subrole,
        "title": _string_or_none(copy_ax_attribute(element, "AXTitle")),
        "description": _string_or_none(copy_ax_attribute(element, "AXDescription")),
        "identifier": _string_or_none(copy_ax_attribute(element, "AXIdentifier")),
        "protected": bool(protected) if protected is not None else False,
        "frame": _frame_value_to_dict(frame),
    }
    node["value"] = (
        _string_or_none(copy_ax_attribute(element, "AXValue"))
        if include_value and not is_secure
        else None
    )
    return node


def copy_ax_attribute(element, attribute: str):
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
    except Exception:
        return None


def _string_or_none(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)


def _frame_value_to_dict(value) -> dict | None:
    if isinstance(value, dict):
        return value
    if isinstance(value, AXFrame):
        return value.to_dict()
    if value is None:
        return None
    try:
        return {
            "x": float(value.origin.x),
            "y": float(value.origin.y),
            "width": float(value.size.width),
            "height": float(value.size.height),
        }
    except Exception:
        return None
