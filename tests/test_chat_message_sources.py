from datetime import datetime
import sys
import threading
from types import SimpleNamespace

import pytest

from ominime import chat_message_sources
from ominime.chat_bubble_capture import VisualBubbleSource
from ominime.chat_message_sources import (
    AXMessageNode,
    AccessibilityBubbleSource,
    default_message_sources,
)
from ominime.chat_window_capture import WindowFrame
from ominime.kim_composer_capture import NormalizedRect
from ominime.post_send_capture import SendIntent, build_default_message_source_chain


def _intent(*, bundle_id="Kem"):
    return SendIntent(
        intent_id="send-1",
        submitted_at=10.0,
        timestamp=datetime(2026, 8, 15, 12, 0),
        app_name="Kim" if bundle_id == "Kem" else "WeChat",
        bundle_id=bundle_id,
        target_pid=123,
        modifiers={},
        physical_key_count=2,
        validation_text="最终文本",
        baseline=WindowFrame(
            "before", 42, 123, 1000, 800, 9.9, "session-a"
        ),
    )


def _node(
    text="最终文本",
    *,
    pid=123,
    role="AXStaticText",
    identity="node-1",
    observed_at=10.2,
    newly_observed=True,
    bounds=NormalizedRect(0.72, 0.36, 0.20, 0.04),
    secure=False,
    session_anchor="session-a",
    window_id=42,
    children=(),
):
    return AXMessageNode(
        target_pid=pid,
        role=role,
        text=text,
        identity=identity,
        observed_at=observed_at,
        newly_observed=newly_observed,
        bounds=bounds,
        secure=secure,
        session_anchor=session_anchor,
        window_id=window_id,
        children=children,
    )


def test_accessibility_source_requires_complete_new_outgoing_identity():
    source = AccessibilityBubbleSource(node_provider=lambda pid: (_node(),))

    result = source.read(_intent())

    assert result.content == "最终文本"
    assert result.source == "kim_postsend_ax"
    assert result.message_identity == "ax:123:node-1"
    assert result.stability_key == "node-1"


@pytest.mark.parametrize(
    ("node", "failure"),
    [
        (_node(pid=456), "ax_target_pid_mismatch"),
        (_node(role="AXButton"), "ax_not_static_text"),
        (_node(observed_at=9.9), "ax_stale_node"),
        (_node(newly_observed=False), "ax_not_new"),
        (
            _node(bounds=NormalizedRect(0.25, 0.36, 0.20, 0.04)),
            "ax_not_outgoing",
        ),
        (_node(identity=""), "ax_missing_identity"),
        (_node(session_anchor="session-b"), "ax_session_anchor_mismatch"),
        (_node(window_id=99), "ax_window_identity_mismatch"),
        (_node(text="不匹配"), "ax_validation_mismatch"),
        (_node(secure=True), "ax_secure_element"),
        (_node(text="  "), "ax_empty_text"),
    ],
)
def test_accessibility_source_rejects_incomplete_evidence(node, failure):
    source = AccessibilityBubbleSource(node_provider=lambda pid: (node,))

    result = source.read(_intent())

    assert result.failure_reason == failure
    assert result.content == ""


def test_accessibility_source_handles_unavailable_tree_and_native_exception(
    caplog,
):
    unavailable = AccessibilityBubbleSource(node_provider=lambda pid: ())

    def fail_native(pid):
        raise RuntimeError("private AX value")

    failed = AccessibilityBubbleSource(node_provider=fail_native)

    assert unavailable.read(_intent()).failure_reason == "ax_tree_unavailable"
    result = failed.read(_intent())
    assert result.failure_reason == "ax_native_error"
    assert "private AX value" not in repr(result)
    assert "private AX value" not in caplog.text


def test_accessibility_source_traverses_children_but_is_bounded():
    outgoing = _node()
    root = _node(
        text="",
        role="AXGroup",
        identity="root",
        newly_observed=False,
        children=(outgoing,),
    )
    source = AccessibilityBubbleSource(
        node_provider=lambda pid: (root,),
        max_nodes=2,
    )

    assert source.read(_intent()).content == "最终文本"

    too_small = AccessibilityBubbleSource(
        node_provider=lambda pid: (root,),
        max_nodes=1,
    )
    assert too_small.read(_intent()).failure_reason == "ax_traversal_limit"


def test_accessibility_source_cooperatively_cancels_provider():
    cancelled = threading.Event()

    class Provider:
        def __call__(self, pid):
            return ()

        def cancel(self):
            cancelled.set()

    source = AccessibilityBubbleSource(node_provider=Provider())

    source.cancel()

    assert cancelled.is_set()


def test_wechat_accessibility_source_uses_explicit_source_name():
    source = AccessibilityBubbleSource(node_provider=lambda pid: (_node(),))

    result = source.read(_intent(bundle_id="com.tencent.xinWeChat"))

    assert result.source == "wechat_postsend_ax"


def test_unknown_bundle_accessibility_source_uses_generic_prefix():
    source = AccessibilityBubbleSource(node_provider=lambda pid: (_node(),))

    result = source.read(_intent(bundle_id="com.example.unknown"))

    assert result.source == "generic_postsend_ax"


def test_default_sources_are_ax_then_vision_without_network_probe(monkeypatch):
    network_calls = []
    monkeypatch.setattr(
        "socket.create_connection",
        lambda *args, **kwargs: network_calls.append((args, kwargs)),
    )
    sources = default_message_sources(
        frame_provider=lambda pid: None,
        ax_node_provider=lambda pid: (),
        ocr_provider=lambda image, bounds: (),
        difference_provider=lambda before, after, bounds: (),
    )

    assert isinstance(sources[0], AccessibilityBubbleSource)
    assert isinstance(sources[1], VisualBubbleSource)
    assert network_calls == []


def test_default_sources_preload_vision_without_reading_an_image(monkeypatch):
    prepared = []
    image_reads = []

    class FakeNativeRecognizer:
        def prepare(self):
            prepared.append(True)

        def __call__(self, image, bounds):
            image_reads.append(image)
            return ()

    monkeypatch.setattr(
        chat_message_sources,
        "VisionTextRecognizer",
        FakeNativeRecognizer,
        raising=False,
    )

    sources = default_message_sources(
        frame_provider=lambda pid: None,
        difference_provider=lambda before, after, bounds: (),
    )

    assert prepared == [True]
    assert image_reads == []
    assert isinstance(sources[1], VisualBubbleSource)


def test_default_native_ax_probe_fails_closed_without_reading_tree(monkeypatch):
    ax_reads = []
    fake_ax = SimpleNamespace(
        AXUIElementCreateApplication=lambda pid: ax_reads.append(("app", pid)),
        AXUIElementCopyAttributeValue=lambda *args: ax_reads.append(("value", args)),
    )
    monkeypatch.setitem(sys.modules, "ApplicationServices", fake_ax)
    source = AccessibilityBubbleSource()

    snapshots = source._native_provider(123)

    assert snapshots == ()
    assert ax_reads == []


def test_default_chain_prefers_ax_before_visual_fallback():
    visual_calls = []
    chain = build_default_message_source_chain(
        frame_provider=lambda pid: visual_calls.append(pid),
        ax_node_provider=lambda pid: (_node(),),
        ocr_provider=lambda image, bounds: (),
        difference_provider=lambda before, after, bounds: (),
    )

    result = chain.read(_intent())

    assert result.source == "kim_postsend_ax"
    assert visual_calls == []
