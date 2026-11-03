from datetime import datetime

import pytest

from ominime.chat_bubble_capture import (
    GENERIC_BUBBLE_PROFILE,
    KIM_BUBBLE_PROFILE,
    WECHAT_BUBBLE_PROFILE,
    VisualBubbleSource,
    bubble_profile_for_bundle,
)
from ominime.chat_window_capture import WindowFrame
from ominime.kim_composer_capture import NormalizedRect, RecognizedLine
from ominime.post_send_capture import MAX_TRUSTED_SUBMISSION_CHARS, SendIntent


def _frame(
    image="current",
    *,
    window_id=42,
    width=1000,
    height=800,
    session_anchor="session-a",
):
    return WindowFrame(
        image, window_id, 123, width, height, 10.2, session_anchor
    )


def _intent(
    *,
    app_name="Kim",
    bundle_id="Kem",
    baseline=None,
    intent_id="send-1",
    validation_text="",
):
    return SendIntent(
        intent_id=intent_id,
        submitted_at=10.0,
        timestamp=datetime(2026, 8, 15, 12, 0),
        app_name=app_name,
        bundle_id=bundle_id,
        target_pid=123,
        modifiers={},
        physical_key_count=4,
        validation_text=validation_text,
        baseline=baseline or _frame("baseline"),
    )


def _source(lines, changed, *, current=None, clock=lambda: 10.25):
    return VisualBubbleSource(
        frame_provider=lambda pid: current or _frame(),
        ocr_provider=lambda image, bounds: tuple(lines),
        difference_provider=lambda before, after, bounds: tuple(changed),
        clock=clock,
    )


def test_profiles_derive_search_bounds_from_actual_frame_dimensions():
    kim = KIM_BUBBLE_PROFILE.search_bounds(_frame(width=1000, height=800))
    wechat_small = WECHAT_BUBBLE_PROFILE.search_bounds(
        _frame(width=880, height=640)
    )
    wechat_large = WECHAT_BUBBLE_PROFILE.search_bounds(
        _frame(width=1200, height=900)
    )

    assert tuple(round(value, 3) for value in (kim.x, kim.y, kim.width, kim.height)) == (
        0.28,
        0.20,
        0.70,
        0.71,
    )
    assert round(wechat_small.x * 880) == 305
    assert round(wechat_large.x * 1200) == 305
    assert round(wechat_small.y * 640) == 146
    assert round(wechat_large.y * 900) == 146
    assert round((1 - wechat_small.y - wechat_small.height) * 640) == 64
    assert round((1 - wechat_large.y - wechat_large.height) * 900) == 64


def test_visual_source_excludes_header_sidebar_and_composer_chrome():
    lines = (
        RecognizedLine("顶部导航", 0.5, 0.95, 0.2, 0.03),
        RecognizedLine("左侧会话", 0.05, 0.5, 0.15, 0.03),
        RecognizedLine("底部输入", 0.6, 0.05, 0.2, 0.03),
    )
    source = _source(lines, (NormalizedRect(0, 0, 1, 1),))

    result = source.read(_intent())

    assert result.failure_reason == "ocr_no_outgoing_bubble"
    assert result.content == ""


def test_visual_source_preserves_wrapped_line_order_in_outgoing_bubble():
    lines = (
        RecognizedLine("第二行", 0.72, 0.48, 0.20, 0.035),
        RecognizedLine("第一行", 0.70, 0.54, 0.22, 0.035),
    )
    changed = (NormalizedRect(0.68, 0.46, 0.27, 0.14),)

    result = _source(lines, changed).read(
        _intent(validation_text="第一行\n第二行")
    )

    assert result.content == "第一行\n第二行"
    assert result.source == "kim_postsend_ocr"
    assert result.stability_key is not None


def test_visual_source_rejects_incoming_and_unchanged_old_bubbles():
    incoming = RecognizedLine("对方消息", 0.32, 0.5, 0.20, 0.04)
    old_outgoing = RecognizedLine("旧消息", 0.74, 0.65, 0.18, 0.04)
    unrelated_change = NormalizedRect(0.70, 0.3, 0.2, 0.08)

    result = _source(
        (incoming, old_outgoing),
        (unrelated_change,),
    ).read(_intent())

    assert result.failure_reason == "ocr_no_new_outgoing_bubble"


def test_visual_source_accepts_lowest_new_right_side_bubble():
    lines = (
        RecognizedLine("旧消息", 0.74, 0.68, 0.18, 0.04),
        RecognizedLine("最终文本", 0.72, 0.36, 0.20, 0.04),
        RecognizedLine("对方插话", 0.34, 0.28, 0.20, 0.04),
    )
    changed = (NormalizedRect(0.70, 0.34, 0.24, 0.09),)

    result = _source(lines, changed).read(
        _intent(validation_text="最终文本")
    )

    assert result.content == "最终文本"
    assert result.message_identity.startswith("send-1:123:42:")


def test_visual_source_does_not_group_adjacent_unchanged_old_bubble():
    lines = (
        RecognizedLine("旧消息", 0.72, 0.42, 0.20, 0.04),
        RecognizedLine("最终文本", 0.72, 0.36, 0.20, 0.04),
    )
    changed = (NormalizedRect(0.70, 0.34, 0.24, 0.07),)

    result = _source(lines, changed).read(
        _intent(validation_text="最终文本")
    )

    assert result.content == "最终文本"


def test_visual_source_rejects_coarse_multiline_change_without_validation():
    lines = (
        RecognizedLine("旧消息", 0.72, 0.42, 0.20, 0.04),
        RecognizedLine("最终文本", 0.72, 0.36, 0.20, 0.04),
    )
    changed = (NormalizedRect(0.70, 0.34, 0.24, 0.14),)

    result = _source(lines, changed).read(_intent())

    assert result.failure_reason == "ocr_validation_unavailable"


def test_visual_source_rejects_validation_text_mismatch():
    line = RecognizedLine("另一条消息", 0.72, 0.36, 0.20, 0.04)

    result = _source(
        (line,),
        (NormalizedRect(0.70, 0.34, 0.24, 0.09),),
    ).read(_intent(validation_text="完全不匹配"))

    assert result.failure_reason == "ocr_validation_mismatch"


def test_visual_source_rejects_long_incoming_line_reaching_right_edge():
    incoming = RecognizedLine("很长的对方消息", 0.32, 0.36, 0.62, 0.04)

    result = _source(
        (incoming,),
        (NormalizedRect(0.30, 0.34, 0.66, 0.09),),
    ).read(_intent(validation_text="很长的对方消息"))

    assert result.failure_reason == "ocr_no_outgoing_bubble"


def test_identical_consecutive_messages_get_distinct_identities():
    line = RecognizedLine("相同文本", 0.72, 0.36, 0.20, 0.04)
    changed = (NormalizedRect(0.70, 0.34, 0.24, 0.09),)
    observed = iter((10.25, 10.55))
    source = _source((line,), changed, clock=lambda: next(observed))

    first = source.read(
        _intent(intent_id="send-1", validation_text="相同文本")
    )
    second = source.read(
        _intent(intent_id="send-2", validation_text="相同文本")
    )

    assert first.content == second.content == "相同文本"
    assert first.message_identity != second.message_identity


@pytest.mark.parametrize(
    ("lines", "failure"),
    [
        ((RecognizedLine("边缘裁切", 0.70, 0.201, 0.22, 0.04),), "ocr_edge_clipped"),
        (
            tuple(
                RecognizedLine("watermark", 0.70, 0.35 + index * 0.04, 0.2, 0.03)
                for index in range(3)
            ),
            "ocr_repeated_text_untrusted",
        ),
        ((RecognizedLine(" ", 0.72, 0.36, 0.2, 0.04),), "ocr_no_outgoing_bubble"),
        (
            (
                RecognizedLine(
                    "x" * (MAX_TRUSTED_SUBMISSION_CHARS + 1),
                    0.70,
                    0.36,
                    0.22,
                    0.04,
                ),
            ),
            "ocr_content_too_long",
        ),
    ],
)
def test_visual_source_rejects_untrusted_candidates(lines, failure):
    result = _source(
        lines,
        (NormalizedRect(0.68, 0.19, 0.28, 0.40),),
    ).read(_intent())

    assert result.failure_reason == failure
    assert result.content == ""


def test_post_send_bubble_accepts_committed_latin_without_doubao_preedit_rules():
    line = RecognizedLine("ceshi", 0.72, 0.36, 0.20, 0.04)

    result = _source(
        (line,),
        (NormalizedRect(0.70, 0.34, 0.24, 0.09),),
    ).read(_intent(validation_text="ceshi"))

    assert result.content == "ceshi"


def test_visual_source_rejects_single_line_without_validation_summary():
    line = RecognizedLine("可能是旧消息", 0.72, 0.36, 0.20, 0.04)

    result = _source(
        (line,),
        (NormalizedRect(0.60, 0.25, 0.36, 0.30),),
    ).read(_intent())

    assert result.failure_reason == "ocr_validation_unavailable"


def test_visual_failure_diagnostic_never_contains_raw_candidate_text():
    private_text = "private-candidate-123"
    line = RecognizedLine(private_text, 0.30, 0.4, 0.20, 0.04)

    result = _source(
        (line,),
        (NormalizedRect(0.28, 0.38, 0.24, 0.09),),
    ).read(_intent())

    assert result.failure_reason == "ocr_no_outgoing_bubble"
    assert private_text not in repr(result)


def test_visual_native_exception_log_does_not_include_private_text(caplog):
    private_text = "private OCR value"

    def fail_frame(pid):
        raise RuntimeError(private_text)

    result = VisualBubbleSource(frame_provider=fail_frame).read(_intent())

    assert result.failure_reason == "ocr_native_error"
    assert private_text not in caplog.text


def test_visual_source_rejects_mismatched_current_window():
    source = _source(
        (RecognizedLine("错误窗口", 0.72, 0.36, 0.20, 0.04),),
        (NormalizedRect(0.70, 0.34, 0.24, 0.09),),
        current=_frame(window_id=99),
    )

    result = source.read(_intent())

    assert result.failure_reason == "window_identity_mismatch"


def test_wechat_visual_source_accepts_window_recreation_with_same_session_anchor():
    line = RecognizedLine("T", 0.82, 0.36, 0.08, 0.04)
    source = _source(
        (line,),
        (NormalizedRect(0.80, 0.34, 0.12, 0.09),),
        current=_frame(window_id=99),
    )

    result = source.read(
        _intent(
            app_name="微信",
            bundle_id="com.tencent.xinWeChat",
            validation_text="T",
        )
    )

    assert result.content == "T"
    assert result.source == "wechat_postsend_ocr"
    assert result.window_id == 42


def test_generic_profile_reserves_header_and_composer_bands():
    bounds = GENERIC_BUBBLE_PROFILE.search_bounds(_frame(width=1000, height=800))

    assert (bounds.x, bounds.y, bounds.width, bounds.height) == (0.0, 0.18, 1.0, 0.78)


def test_bubble_profile_registry_routes_known_and_unknown_bundles():
    assert bubble_profile_for_bundle("com.tencent.xinWeChat") is WECHAT_BUBBLE_PROFILE
    assert bubble_profile_for_bundle("com.tencent.WeChat") is WECHAT_BUBBLE_PROFILE
    assert bubble_profile_for_bundle("Kem") is KIM_BUBBLE_PROFILE
    assert bubble_profile_for_bundle("com.electron.kim") is KIM_BUBBLE_PROFILE
    assert bubble_profile_for_bundle("md.obsidian") is GENERIC_BUBBLE_PROFILE
    assert bubble_profile_for_bundle("") is GENERIC_BUBBLE_PROFILE


def test_visual_source_generic_profile_emits_generic_source():
    line = RecognizedLine("最终文本", 0.80, 0.36, 0.12, 0.04)

    result = _source(
        (line,),
        (NormalizedRect(0.78, 0.34, 0.16, 0.09),),
    ).read(
        _intent(
            app_name="随便什么",
            bundle_id="com.example.unknown",
            validation_text="最终文本",
        )
    )

    assert result.content == "最终文本"
    assert result.source == "generic_postsend_ocr"


def test_visual_source_rejects_window_recreation_for_unknown_bundle():
    line = RecognizedLine("T", 0.80, 0.36, 0.12, 0.04)
    source = _source(
        (line,),
        (NormalizedRect(0.78, 0.34, 0.16, 0.09),),
        current=_frame(window_id=99),
    )

    result = source.read(
        _intent(app_name="随便什么", bundle_id="com.example.unknown", validation_text="T")
    )

    assert result.failure_reason == "window_identity_mismatch"


def test_visual_source_rejects_same_window_conversation_switch():
    source = _source(
        (RecognizedLine("另一会话", 0.72, 0.36, 0.20, 0.04),),
        (NormalizedRect(0.70, 0.34, 0.24, 0.09),),
        current=_frame(session_anchor="session-b"),
    )

    result = source.read(_intent(validation_text="另一会话"))

    assert result.failure_reason == "session_anchor_mismatch"


def test_visual_source_propagates_cooperative_cancellation():
    cancelled = []

    class CancellableProvider:
        def __call__(self, *args):
            return ()

        def cancel(self):
            cancelled.append(type(self).__name__)

    source = VisualBubbleSource(
        frame_provider=lambda pid: _frame(),
        ocr_provider=CancellableProvider(),
        difference_provider=CancellableProvider(),
    )

    source.cancel()

    assert cancelled == ["CancellableProvider", "CancellableProvider"]
