from pathlib import Path


TEMPLATE = Path("src/ominime/web/templates/index.html")


def test_dashboard_initializes_today_from_status_endpoint():
    html = TEMPLATE.read_text(encoding="utf-8")

    assert "initializeDashboard" in html
    assert "fetch('/api/status')" in html
    assert "serverTodayDate = status.today_date" in html


def test_dashboard_auto_refreshes_when_viewing_today():
    html = TEMPLATE.read_text(encoding="utf-8")

    assert "setInterval(refreshTodayView" in html
    assert "datePicker.value === today" in html
    assert "datePicker.value === previousTodayDate" in html


def test_dashboard_overview_uses_lightweight_stats_endpoints():
    html = TEMPLATE.read_text(encoding="utf-8")

    assert "buildOverviewFromAppStats" in html
    assert "fetch(`/api/stats/apps?target_date=${dateStr}`)" in html


def test_dashboard_escapes_dynamic_app_and_work_path_labels():
    html = TEMPLATE.read_text(encoding="utf-8")

    assert "${escapeHtml(app.display_name)}" in html
    assert "${escapeHtml(workPath.work_pattern)}" in html
    assert "${escapeHtml(p.app)}" in html


def test_dashboard_does_not_contact_google_fonts():
    html = TEMPLATE.read_text(encoding="utf-8")

    assert "fonts.googleapis.com" not in html
    assert "fonts.gstatic.com" not in html


def test_dashboard_loads_no_remote_runtime_resources():
    html = TEMPLATE.read_text(encoding="utf-8")

    assert '<script src="http' not in html
    assert '<link href="http' not in html
    assert "new Chart(" not in html


def test_dashboard_has_no_qwen_multimodal_panel():
    html = TEMPLATE.read_text(encoding="utf-8")

    assert "Qwen 多模态识别" not in html
    assert "qwen_analysis" not in html
    assert "qwen_model" not in html


def test_dashboard_has_no_submission_context_surface():
    html = TEMPLATE.read_text(encoding="utf-8")

    assert "🧠 上下文" not in html
    assert 'id="tab-context"' not in html
    assert "loadSubmissionContexts" not in html
    assert "renderSubmissionContext" not in html
    assert "/api/submissions" not in html
    assert ".context-card" not in html


def test_dashboard_describes_local_only_ai():
    html = TEMPLATE.read_text(encoding="utf-8")

    assert "AI 分析仅使用本地模型" in html
    assert "OpenAI API Key" not in html
    assert "远程 AI" not in html


def test_dashboard_does_not_present_an_efficiency_score():
    html = TEMPLATE.read_text(encoding="utf-8")

    assert "效率分数" not in html
    assert "workPath.efficiency_score" not in html


def test_dashboard_does_not_present_unmeasured_active_time():
    html = TEMPLATE.read_text(encoding="utf-8")

    assert "活跃时间" not in html
    assert "todayTime" not in html


def test_submission_cards_are_not_shrunk_inside_scroll_container():
    """Regression: 40+ rows must not collapse to thin horizontal stripes."""
    import re

    html = TEMPLATE.read_text(encoding="utf-8")
    match = re.search(r"\\.submission-entry\\s*\\{([^}]*)\\}", html)
    assert match is not None
    assert "flex: 0 0 auto" in match.group(1)
    assert "overflow-y: auto" in html


def test_clicking_submission_text_does_not_collapse_app():
    html = TEMPLATE.read_text(encoding="utf-8")

    assert 'onclick="toggleAppContent(this.parentElement)"' in html
    assert 'onclick="toggleAppContent(this)"' not in html
