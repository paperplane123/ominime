"""Regression coverage for per-submission display and Safari count-only diagnostics."""

from datetime import datetime

from fastapi.testclient import TestClient

from ominime.database import CaptureDiagnosticRecord, Database, InputRecord
from ominime.web import api as web_api


def add_input(db, *, timestamp, app, bundle, text, count):
    return db.save_input_record(
        InputRecord(
            id=None,
            timestamp=timestamp,
            app_name=app,
            app_bundle_id=bundle,
            display_name=app,
            content=text,
            char_count=count,
            session_id=f"{bundle}-{timestamp.isoformat()}",
            duration_seconds=0,
        )
    )


def test_content_by_app_keeps_each_submission_and_count_only_label(tmp_path, monkeypatch):
    db = Database(tmp_path / "test.db")
    monkeypatch.setattr(web_api, "get_database", lambda: db)
    same_second = datetime(2026, 10, 30, 12, 34, 56)

    # Equal timestamps still have deterministic insertion order by row ID.
    add_input(
        db, timestamp=same_second, app="微信",
        bundle="com.tencent.xinWeChat", text="今天天气", count=4,
    )
    add_input(
        db, timestamp=same_second, app="微信",
        bundle="com.tencent.xinWeChat", text="不错", count=2,
    )
    add_input(
        db, timestamp=same_second, app="Safari",
        bundle="com.apple.Safari", text="", count=51,
    )

    response = TestClient(web_api.app).get(
        "/api/content/by-app?target_date=2026-10-30"
    )
    assert response.status_code == 200
    apps = {entry["app_bundle_id"]: entry for entry in response.json()}

    wechat = apps["com.tencent.xinWeChat"]
    assert wechat["session_count"] == 2
    assert [row["content"] for row in wechat["sessions"]] == ["今天天气", "不错"]
    assert [row["time"] for row in wechat["sessions"]] == [
        "12:34:56", "12:34:56"
    ]
    assert [row["id"] for row in wechat["sessions"]] == sorted(
        row["id"] for row in wechat["sessions"]
    )
    assert wechat["full_content"] == "今天天气\n\n不错"
    assert wechat["recorded_text_chars"] == 6
    assert wechat["estimated_key_count"] == 0

    safari = apps["com.apple.Safari"]
    assert safari["session_count"] == 1
    assert safari["recorded_text_chars"] == 0
    assert safari["estimated_key_count"] == 51
    assert safari["sessions"][0]["count_only"] is True
    assert safari["sessions"][0]["content"] == ""


def test_capture_diagnostics_can_be_filtered_per_app(tmp_path, monkeypatch):
    db = Database(tmp_path / "test.db")
    monkeypatch.setattr(web_api, "get_database", lambda: db)
    for name, bundle, action in [
        ("微信", "com.tencent.xinWeChat", "persist_text"),
        ("Safari", "com.apple.Safari", "persist_count"),
    ]:
        db.save_capture_diagnostic(
            CaptureDiagnosticRecord(
                id=None,
                timestamp=datetime(2026, 10, 30, 12, 34, 56),
                app_name=name,
                app_bundle_id=bundle,
                event_type="enter_keydown",
                decision_action=action,
                decision_reason="saved_submission",
                selected_source="count_unreadable" if action == "persist_count" else "ax_value",
                selected_confidence=None,
                physical_key_count=51 if action == "persist_count" else 6,
                focused_role="AXTextField",
                focused_subrole=None,
                capture_status="ok",
                diagnostics_json="{}",
            )
        )

    response = TestClient(web_api.app).get(
        "/api/capture/diagnostics?app_bundle_id=com.apple.Safari&limit=20"
    )
    assert response.status_code == 200
    rows = response.json()["diagnostics"]
    assert len(rows) == 1
    assert rows[0]["app_bundle_id"] == "com.apple.Safari"
    assert rows[0]["decision_action"] == "persist_count"
