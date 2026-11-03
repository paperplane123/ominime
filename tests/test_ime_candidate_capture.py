from collections import UserDict, UserList
import threading

from ominime import ime_candidate_capture

from ominime.ime_candidate_capture import (
    CandidateSnapshot,
    DOUBAO_BUNDLE_ID,
    DoubaoCandidateReader,
    DoubaoCompositionState,
    IMECandidateReader,
    ImeCandidateProfile,
    Rect,
    WindowRecord,
    candidate_is_in_composer,
    collect_static_text_values,
    default_ime_profile_resolver,
    object_sequence,
    rect_from_window_bounds,
)


def test_candidate_inside_lower_target_region_is_trusted():
    target = Rect(x=2000, y=100, width=1200, height=800)
    candidate = Rect(x=2500, y=760, width=400, height=64)

    assert candidate_is_in_composer(candidate, target)


def test_input_source_cache_is_empty_until_refreshed_or_when_expired(monkeypatch):
    monkeypatch.setattr(
        ime_candidate_capture,
        "_input_source_snapshot",
        ("", 0.0),
        raising=False,
    )

    assert ime_candidate_capture.cached_input_source_bundle_id(
        clock=lambda: 100.0,
    ) == ""

    ime_candidate_capture.refresh_input_source_cache(
        native_provider=lambda: DOUBAO_BUNDLE_ID,
        clock=lambda: 100.0,
    )

    assert ime_candidate_capture.cached_input_source_bundle_id(
        clock=lambda: 100.5,
    ) == DOUBAO_BUNDLE_ID
    assert ime_candidate_capture.cached_input_source_bundle_id(
        clock=lambda: 102.0,
    ) == ""


def test_failed_input_source_refresh_replaces_old_identity_with_empty(monkeypatch):
    monkeypatch.setattr(
        ime_candidate_capture,
        "_input_source_snapshot",
        ("", 0.0),
        raising=False,
    )
    ime_candidate_capture.refresh_input_source_cache(
        native_provider=lambda: DOUBAO_BUNDLE_ID,
        clock=lambda: 100.0,
    )

    def fail_native_read():
        raise RuntimeError("native read failed")

    assert ime_candidate_capture.refresh_input_source_cache(
        native_provider=fail_native_read,
        clock=lambda: 100.25,
    ) == ""
    assert ime_candidate_capture.cached_input_source_bundle_id(
        clock=lambda: 100.5,
    ) == ""


def test_worker_refresh_is_rejected_before_native_provider_runs():
    native_calls = []
    errors = []

    def refresh_on_worker():
        try:
            ime_candidate_capture.refresh_input_source_cache(
                native_provider=lambda: native_calls.append(True) or DOUBAO_BUNDLE_ID,
            )
        except Exception as error:
            errors.append(error)

    worker = threading.Thread(target=refresh_on_worker)
    worker.start()
    worker.join()

    assert native_calls == []
    assert len(errors) == 1
    assert isinstance(errors[0], RuntimeError)


def test_direct_native_input_source_read_is_rejected_on_worker(monkeypatch):
    native_calls = []
    errors = []
    monkeypatch.setattr(
        ime_candidate_capture,
        "_native_input_source_api",
        lambda: native_calls.append(True),
    )

    def read_on_worker():
        try:
            ime_candidate_capture.current_input_source_bundle_id()
        except Exception as error:
            errors.append(error)

    worker = threading.Thread(target=read_on_worker)
    worker.start()
    worker.join()

    assert native_calls == []
    assert len(errors) == 1
    assert isinstance(errors[0], RuntimeError)


def test_candidate_near_top_search_field_is_rejected():
    target = Rect(x=2000, y=100, width=1200, height=800)
    candidate = Rect(x=2500, y=180, width=400, height=64)

    assert not candidate_is_in_composer(candidate, target)


def test_candidate_outside_target_horizontally_is_rejected():
    target = Rect(x=2000, y=100, width=1200, height=800)
    candidate = Rect(x=3150, y=760, width=400, height=64)

    assert not candidate_is_in_composer(candidate, target)


def test_candidate_below_target_is_rejected():
    target = Rect(x=2000, y=100, width=1200, height=800)
    candidate = Rect(x=2500, y=900, width=400, height=64)

    assert not candidate_is_in_composer(candidate, target)


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def candidate_snapshot(clock, candidates=("测试", "策士", "侧室"), target_pid=123):
    return CandidateSnapshot(candidates, target_pid, clock())


def test_composition_space_commits_default_candidate_without_pinyin():
    clock = FakeClock()
    state = DoubaoCompositionState(timeout_seconds=30, clock=clock)
    for character in "ceshi":
        state.record_printable(character, target_pid=123)
    state.update_candidates(candidate_snapshot(clock), target_pid=123)

    result = state.handle_key(keycode=49, text=" ", target_pid=123)

    assert result.candidate_committed
    assert result.committed_text == "测试"
    assert state.pop_submission(target_pid=123) == "测试"


def test_composition_number_key_commits_matching_candidate():
    clock = FakeClock()
    state = DoubaoCompositionState(timeout_seconds=30, clock=clock)
    state.record_printable("c", target_pid=123)
    state.update_candidates(candidate_snapshot(clock), target_pid=123)

    result = state.handle_key(keycode=19, text="2", target_pid=123)

    assert result.candidate_committed
    assert result.committed_text == "策士"


def test_composition_arrow_keys_move_and_clamp_selection():
    clock = FakeClock()
    state = DoubaoCompositionState(timeout_seconds=30, clock=clock)
    state.update_candidates(candidate_snapshot(clock), target_pid=123)

    state.handle_key(keycode=123, text="", target_pid=123)
    assert state.selected_index == 0
    state.handle_key(keycode=124, text="", target_pid=123)
    state.handle_key(keycode=124, text="", target_pid=123)
    state.handle_key(keycode=124, text="", target_pid=123)

    assert state.selected_index == 2


def test_composition_enter_commits_candidate_before_next_enter_submits():
    clock = FakeClock()
    state = DoubaoCompositionState(timeout_seconds=30, clock=clock)
    state.record_printable("c", target_pid=123)
    state.update_candidates(candidate_snapshot(clock), target_pid=123)

    result = state.handle_key(keycode=36, text="", target_pid=123)

    assert result.candidate_committed
    assert state.has_active_candidate is False
    assert state.pop_submission(target_pid=123) == "测试"
    assert state.pop_submission(target_pid=123) == ""


def test_composition_backspace_edits_preedit_then_confirmed_text():
    clock = FakeClock()
    state = DoubaoCompositionState(timeout_seconds=30, clock=clock)
    state.record_printable("c", target_pid=123)
    state.record_printable("e", target_pid=123)

    state.handle_key(keycode=51, text="", target_pid=123)
    assert state.pending_preedit == "c"
    state.update_candidates(candidate_snapshot(clock), target_pid=123)
    state.handle_key(keycode=49, text=" ", target_pid=123)
    state.handle_key(keycode=51, text="", target_pid=123)

    assert state.confirmed_text == "测"


def test_composition_never_submits_unconfirmed_raw_pinyin():
    clock = FakeClock()
    state = DoubaoCompositionState(timeout_seconds=30, clock=clock)
    for character in "ceshi":
        state.record_printable(character, target_pid=123)

    assert state.pop_submission(target_pid=123) == ""


def test_composition_does_not_promote_latin_after_trusted_candidate_session():
    clock = FakeClock()
    state = DoubaoCompositionState(timeout_seconds=30, clock=clock)
    state.record_printable("c", target_pid=123)
    state.update_candidates(candidate_snapshot(clock), target_pid=123)
    state.handle_key(keycode=49, text=" ", target_pid=123)
    state.record_printable("A", target_pid=123)

    state.update_candidates(None, target_pid=123)

    assert state.pop_submission(target_pid=123) == ""


def test_composition_discards_pinyin_when_active_candidate_disappears():
    clock = FakeClock()
    state = DoubaoCompositionState(timeout_seconds=5, clock=clock)
    for character in "ceshi":
        state.record_printable(character, target_pid=123)
    state.update_candidates(candidate_snapshot(clock), target_pid=123)

    state.update_candidates(None, target_pid=123)

    assert state.pop_submission(target_pid=123) == ""


def test_composition_rejects_stale_candidate_snapshot():
    clock = FakeClock()
    state = DoubaoCompositionState(timeout_seconds=5, clock=clock)
    stale = CandidateSnapshot(("测试",), 123, clock() - 6)

    state.update_candidates(stale, target_pid=123)

    assert not state.has_active_candidate


def test_composition_pid_change_discards_previous_content():
    clock = FakeClock()
    state = DoubaoCompositionState(timeout_seconds=30, clock=clock)
    state.update_candidates(candidate_snapshot(clock), target_pid=123)
    state.handle_key(keycode=49, text=" ", target_pid=123)

    state.record_printable("x", target_pid=456)

    assert state.pop_submission(target_pid=456) == ""


def test_composition_timeout_discards_sensitive_content():
    clock = FakeClock()
    state = DoubaoCompositionState(timeout_seconds=30, clock=clock)
    state.update_candidates(candidate_snapshot(clock), target_pid=123)
    state.handle_key(keycode=49, text=" ", target_pid=123)
    clock.now += 31

    assert state.pop_submission(target_pid=123) == ""


def test_confirmed_message_outlives_short_candidate_snapshot_ttl():
    clock = FakeClock()
    state = DoubaoCompositionState(
        timeout_seconds=300,
        candidate_timeout_seconds=5,
        clock=clock,
    )
    state.update_candidates(candidate_snapshot(clock), target_pid=123)
    state.handle_key(keycode=49, text=" ", target_pid=123)
    clock.now += 6

    assert state.pop_submission(target_pid=123) == "测试"


def test_reader_collects_static_text_in_order_and_deduplicates():
    attributes = {
        ("window", "AXRole"): "AXWindow",
        ("window", "AXChildren"): ["first", "duplicate", "button", "second"],
        ("first", "AXRole"): "AXStaticText",
        ("first", "AXValue"): "测试",
        ("duplicate", "AXRole"): "AXStaticText",
        ("duplicate", "AXValue"): "测试",
        ("button", "AXRole"): "AXButton",
        ("button", "AXValue"): "不应采集",
        ("second", "AXRole"): "AXStaticText",
        ("second", "AXValue"): "策士",
    }

    values = collect_static_text_values(
        ["window"],
        lambda node, attribute: attributes.get((node, attribute)),
    )

    assert values == ("测试", "策士")


def test_reader_static_text_traversal_is_bounded():
    children = [f"child-{index}" for index in range(10)]
    attributes = {
        ("window", "AXChildren"): children,
        ("child-9", "AXRole"): "AXStaticText",
        ("child-9", "AXValue"): "太深",
    }

    values = collect_static_text_values(
        ["window"],
        lambda node, attribute: attributes.get((node, attribute)),
        max_nodes=5,
    )

    assert values == ()


def test_reader_accepts_objective_c_style_sequences():
    children = UserList(["candidate"])
    attributes = {
        ("window", "AXChildren"): children,
        ("candidate", "AXRole"): "AXStaticText",
        ("candidate", "AXValue"): "测试",
    }

    assert object_sequence(children) == ("candidate",)
    assert collect_static_text_values(
        ["window"],
        lambda node, attribute: attributes.get((node, attribute)),
    ) == ("测试",)


def make_reader(
    clock,
    *,
    processes=((DOUBAO_BUNDLE_ID, 88),),
    candidate_bounds=Rect(2500, 760, 400, 64),
    input_source_bundle=DOUBAO_BUNDLE_ID,
):
    attributes = {
        ("window", "AXChildren"): ["first", "second"],
        ("first", "AXRole"): "AXStaticText",
        ("first", "AXValue"): "测试",
        ("second", "AXRole"): "AXStaticText",
        ("second", "AXValue"): "策士",
    }
    windows = [
        WindowRecord(pid=88, bounds=candidate_bounds),
        WindowRecord(pid=123, bounds=Rect(2000, 100, 1200, 800)),
    ]
    return IMECandidateReader(
        clock=clock,
        input_source_provider=lambda: input_source_bundle,
        process_provider=lambda: processes,
        ax_roots_provider=lambda pid: ["window"],
        ax_rect_provider=lambda root: candidate_bounds,
        window_provider=lambda: windows,
        attribute_reader=lambda node, attribute: attributes.get((node, attribute)),
    )


def test_reader_accepts_unknown_target_bundle_and_keeps_geometry_gate():
    clock = FakeClock()
    reader = make_reader(clock)

    snapshot = reader.read(target_pid=123, target_bundle_id="com.example.unknown")

    assert snapshot == CandidateSnapshot(("测试", "策士"), 123, clock())


def test_reader_rejects_unknown_target_by_composer_geometry():
    clock = FakeClock()
    reader = make_reader(clock, candidate_bounds=Rect(2500, 180, 400, 64))

    snapshot = reader.read(target_pid=123, target_bundle_id="com.example.unknown")

    assert snapshot is None
    assert reader.last_failure_reason == "candidate_outside_composer"


def test_registry_resolves_registered_and_unregistered_input_sources():
    assert default_ime_profile_resolver(DOUBAO_BUNDLE_ID) is not None
    assert default_ime_profile_resolver("com.apple.keylayout.ABC") is None


def test_reader_supports_injected_second_ime_profile():
    clock = FakeClock()
    synthetic_profile = ImeCandidateProfile(
        name="otherime",
        bundle_id="com.example.inputmethod.otherime",
    )
    reader = make_reader(
        clock,
        processes=((synthetic_profile.bundle_id, 88),),
        input_source_bundle=synthetic_profile.bundle_id,
    )

    snapshot = reader.read(
        target_pid=123,
        target_bundle_id="com.example.unknown",
        profile_resolver=lambda bundle_id: (
            synthetic_profile if bundle_id == synthetic_profile.bundle_id else None
        ),
    )

    assert snapshot == CandidateSnapshot(("测试", "策士"), 123, clock())


def test_reader_requires_exact_doubao_process_bundle():
    clock = FakeClock()
    reader = make_reader(
        clock,
        processes=(("com.bytedance.inputmethod.lookalike", 88),),
    )

    assert reader.read(target_pid=123, target_bundle_id="Kem") is None


def test_reader_default_uses_cached_input_source_without_native_read(monkeypatch):
    ime_candidate_capture.refresh_input_source_cache(
        native_provider=lambda: DOUBAO_BUNDLE_ID,
    )

    def fail_native_read():
        raise AssertionError("worker path must not read TIS")

    monkeypatch.setattr(
        ime_candidate_capture,
        "current_input_source_bundle_id",
        fail_native_read,
    )
    reader = DoubaoCandidateReader(process_provider=lambda: ())

    assert reader.read(target_pid=123, target_bundle_id="Kem") is None
    assert reader.last_failure_reason == "ime_process_unavailable"


def test_reader_requires_doubao_to_be_current_input_source():
    clock = FakeClock()
    reader = make_reader(clock, input_source_bundle="com.apple.keylayout.ABC")

    assert reader.read(target_pid=123, target_bundle_id="Kem") is None
    assert reader.last_failure_reason == "input_source_mismatch"


def test_reader_rejects_candidate_outside_lower_composer_region():
    clock = FakeClock()
    reader = make_reader(clock, candidate_bounds=Rect(2500, 180, 400, 64))

    assert reader.read(target_pid=123, target_bundle_id="Kem") is None


def test_reader_returns_trusted_snapshot_for_kim_composer():
    clock = FakeClock()
    reader = make_reader(clock)

    snapshot = reader.read(target_pid=123, target_bundle_id="Kem")

    assert snapshot == CandidateSnapshot(("测试", "策士"), 123, clock())


def test_reader_returns_none_when_candidate_ax_values_are_unavailable():
    clock = FakeClock()
    reader = DoubaoCandidateReader(
        clock=clock,
        input_source_provider=lambda: DOUBAO_BUNDLE_ID,
        process_provider=lambda: ((DOUBAO_BUNDLE_ID, 88),),
        ax_roots_provider=lambda pid: ["window"],
        window_provider=lambda: [
            WindowRecord(pid=88, bounds=Rect(2500, 760, 400, 64)),
            WindowRecord(pid=123, bounds=Rect(2000, 100, 1200, 800)),
        ],
        attribute_reader=lambda node, attribute: None,
    )

    assert reader.read(target_pid=123, target_bundle_id="Kem") is None


def test_reader_rejects_text_from_multiple_candidate_ax_windows():
    clock = FakeClock()
    attributes = {
        ("window-one", "AXRole"): "AXStaticText",
        ("window-one", "AXValue"): "测试",
        ("window-two", "AXRole"): "AXStaticText",
        ("window-two", "AXValue"): "其他窗口",
    }
    reader = DoubaoCandidateReader(
        clock=clock,
        input_source_provider=lambda: DOUBAO_BUNDLE_ID,
        process_provider=lambda: ((DOUBAO_BUNDLE_ID, 88),),
        ax_roots_provider=lambda pid: ["window-one", "window-two"],
        ax_rect_provider=lambda root: Rect(2500, 760, 400, 64),
        window_provider=lambda: [
            WindowRecord(pid=88, bounds=Rect(2500, 760, 400, 64)),
            WindowRecord(pid=123, bounds=Rect(2000, 100, 1200, 800)),
        ],
        attribute_reader=lambda node, attribute: attributes.get((node, attribute)),
    )

    assert reader.read(target_pid=123, target_bundle_id="Kem") is None
    assert reader.last_failure_reason == "ambiguous_ax_windows"


def test_reader_ignores_unrelated_quartz_windows_after_ax_window_binding():
    clock = FakeClock()
    reader = make_reader(clock)
    reader._window_provider = lambda: [
        WindowRecord(pid=88, bounds=Rect(2400, 760, 300, 64)),
        WindowRecord(pid=88, bounds=Rect(2750, 760, 300, 64)),
        WindowRecord(pid=123, bounds=Rect(2000, 100, 1200, 800)),
    ]

    assert reader.read(target_pid=123, target_bundle_id="Kem") == CandidateSnapshot(
        ("测试", "策士"),
        123,
        clock(),
    )


def test_reader_binds_ax_text_to_same_window_geometry():
    clock = FakeClock()
    attributes = {
        ("text-window", "AXRole"): "AXStaticText",
        ("text-window", "AXValue"): "不相关文字",
    }
    reader = DoubaoCandidateReader(
        clock=clock,
        input_source_provider=lambda: DOUBAO_BUNDLE_ID,
        process_provider=lambda: ((DOUBAO_BUNDLE_ID, 88),),
        ax_roots_provider=lambda pid: ["text-window", "composer-window"],
        ax_rect_provider=lambda root: (
            Rect(2500, 180, 400, 64)
            if root == "text-window"
            else Rect(2500, 760, 400, 64)
        ),
        window_provider=lambda: [
            WindowRecord(pid=123, bounds=Rect(2000, 100, 1200, 800)),
        ],
        attribute_reader=lambda node, attribute: attributes.get((node, attribute)),
    )

    assert reader.read(target_pid=123, target_bundle_id="Kem") is None
    assert reader.last_failure_reason == "candidate_outside_composer"


def test_reader_parses_macos_mapping_without_requiring_python_dict():
    bounds = UserDict({"X": 2500, "Y": 760, "Width": 400, "Height": 64})

    assert rect_from_window_bounds(bounds) == Rect(2500, 760, 400, 64)
