"""
Web API 模块

提供 RESTful API 接口
"""

from datetime import date, datetime, timedelta
import ipaddress
from typing import List, Optional
from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from pydantic import BaseModel
import os
from pathlib import Path
import json
from urllib.parse import urlsplit

from ..database import get_database, InputRecord
from ..analyzer import get_analyzer
from ..exporter import export_daily_to_obsidian
from ..config import config
from ..runtime_state import get_runtime_state
from ..time_utils import business_today


# 创建 FastAPI 应用
app = FastAPI(
    title="OmniMe API",
    description="macOS 输入追踪系统 Web API",
    version="0.1.0"
)

_LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "testserver"}


def _hostname(authority: str) -> str | None:
    try:
        return urlsplit(f"//{authority}").hostname
    except ValueError:
        return None


@app.middleware("http")
async def enforce_local_same_origin(request, call_next):
    """Keep captured input data behind a loopback, same-origin boundary."""
    client_host = request.client.host if request.client else None
    try:
        client_is_loopback = bool(client_host and ipaddress.ip_address(client_host).is_loopback)
    except ValueError:
        client_is_loopback = client_host == "testclient"
    if not client_is_loopback:
        return JSONResponse({"detail": "local access only"}, status_code=403)

    host = request.headers.get("host", "")
    if _hostname(host) not in _LOCAL_HOSTS:
        return JSONResponse({"detail": "local access only"}, status_code=403)

    origin = request.headers.get("origin")
    if origin:
        parsed_origin = urlsplit(origin)
        if parsed_origin.scheme not in {"http", "https"} or parsed_origin.netloc.lower() != host.lower():
            return JSONResponse({"detail": "cross-origin access denied"}, status_code=403)

    return await call_next(request)


# ===== Pydantic 模型 =====

class AppStatsResponse(BaseModel):
    app_name: str
    display_name: str
    total_chars: int
    session_count: int
    percentage: float


class DailyOverview(BaseModel):
    date: str
    weekday: str
    total_chars: int
    total_apps: int
    total_sessions: int


class WorkPathSegmentResponse(BaseModel):
    start_time: str
    end_time: str
    app_name: str
    display_name: str
    char_count: int
    duration_minutes: float
    content_preview: str


class WorkPathAnalysisResponse(BaseModel):
    total_segments: int
    app_switches: int
    peak_hours: List[dict]
    focus_periods: List[dict]
    work_pattern: str
    segments: Optional[List[WorkPathSegmentResponse]] = None


class DailyReportResponse(BaseModel):
    overview: DailyOverview
    app_stats: List[AppStatsResponse]
    main_activities: List[str]
    summary: str
    suggestions: List[str]
    work_path: Optional[WorkPathAnalysisResponse] = None
    ai_work_analysis: Optional[str] = None


class HourlyStats(BaseModel):
    hour: int
    chars: int


class WeeklyTrend(BaseModel):
    date: str
    weekday: str
    total_chars: int
    app_count: int


class RecordItem(BaseModel):
    id: int
    timestamp: str
    app_name: str
    display_name: str
    content: str
    char_count: int


class StatusResponse(BaseModel):
    status: str
    is_recording: bool
    recording_status: str
    input_capture_mode: str
    today_date: str
    today_chars: int
    last_recorded_at: Optional[str] = None
    db_path: str
    data_dir: str


class ThemeAnalysisResponse(BaseModel):
    themes: List[str]
    work_focus: str
    current_interests: List[str]
    insights: List[str]
    detailed_summary: str


class FullReportResponse(BaseModel):
    overview: DailyOverview
    app_stats: List[AppStatsResponse]
    main_activities: List[str]
    summary: str
    suggestions: List[str]
    work_path: Optional[WorkPathAnalysisResponse] = None
    ai_work_analysis: Optional[str] = None
    theme_analysis: Optional[ThemeAnalysisResponse] = None


def _parse_json_field(value):
    if not value:
        return None
    try:
        return json.loads(value)
    except Exception:
        return value


def _serialize_capture_diagnostic(row: dict | None):
    if not row:
        return None
    return {
        "id": row["id"],
        "timestamp": row["timestamp"],
        "app_name": row["app_name"],
        "app_bundle_id": row["app_bundle_id"],
        "event_type": row["event_type"],
        "decision_action": row["decision_action"],
        "decision_reason": row["decision_reason"],
        "selected_source": row["selected_source"],
        "selected_confidence": row["selected_confidence"],
        "physical_key_count": row["physical_key_count"],
        "focused_role": row["focused_role"],
        "focused_subrole": row["focused_subrole"],
        "capture_status": row["capture_status"],
        "diagnostics": _parse_json_field(row["diagnostics_json"]) or {},
    }


def _build_health_payload() -> dict:
    db = get_database()
    today = business_today()
    yesterday = today - timedelta(days=1)
    latest = db.get_latest_input_record()
    yesterday_stats = db.get_daily_stats(yesterday)
    runtime = get_runtime_state()
    diagnostics = db.get_recent_capture_diagnostics(limit=1)

    return {
        "status": "running",
        "is_recording": runtime.is_recording,
        "recording_status": runtime.recording_status,
        "recording_status_updated_at": (
            runtime.status_updated_at.isoformat()
            if runtime.status_updated_at else None
        ),
        "listener_started_at": (
            runtime.listener_started_at.isoformat()
            if runtime.listener_started_at else None
        ),
        "last_runtime_error": runtime.last_error,
        "process_id": os.getpid(),
        "web_process_id": os.getpid(),
        "listener_process_id": runtime.process_id,
        "runtime_heartbeat_at": (
            runtime.heartbeat_at.isoformat() if runtime.heartbeat_at else None
        ),
        "today_date": today.isoformat(),
        "today_chars": db.get_total_chars_today(),
        "yesterday_date": yesterday.isoformat(),
        "yesterday_chars": sum(s.total_chars for s in yesterday_stats),
        "last_recorded_at": latest.timestamp.isoformat() if latest else None,
        "last_recorded_app": latest.display_name if latest else None,
        "last_recorded_chars": latest.char_count if latest else None,
        "input_capture_mode": getattr(config, "input_capture_mode", "enter-text"),
        "last_capture_diagnostic": _serialize_capture_diagnostic(
            diagnostics[0] if diagnostics else None
        ),
        "db_path": str(config.db_path),
        "data_dir": str(config.data_dir),
    }


# ===== API 路由 =====

@app.get("/api/status", response_model=StatusResponse)
async def get_status():
    """获取系统状态"""
    health = _build_health_payload()
    
    return StatusResponse(
        status=health["status"],
        is_recording=health["is_recording"],
        recording_status=health["recording_status"],
        input_capture_mode=health["input_capture_mode"],
        today_date=health["today_date"],
        today_chars=health["today_chars"],
        last_recorded_at=health["last_recorded_at"],
        db_path=health["db_path"],
        data_dir=health["data_dir"],
    )


@app.get("/api/health")
async def get_health():
    """获取运行状态和最近写入诊断信息"""
    return _build_health_payload()


@app.get("/api/capture/diagnostics")
async def get_capture_diagnostics(
    limit: int = Query(default=50, ge=1, le=500),
    app_bundle_id: Optional[str] = None,
):
    """获取最近的 Enter 捕获决策诊断，可按应用筛选。"""
    db = get_database()
    rows = db.get_recent_capture_diagnostics(
        limit=limit, app_bundle_id=app_bundle_id
    )
    return {
        "total": len(rows),
        "diagnostics": [_serialize_capture_diagnostic(row) for row in rows],
    }


@app.get("/api/overview")
async def get_overview():
    """获取总体概览"""
    db = get_database()
    
    # 今日统计
    today = business_today()
    today_stats = db.get_daily_stats(today)
    today_chars = sum(s.total_chars for s in today_stats)
    
    # 昨日统计
    yesterday = today - timedelta(days=1)
    yesterday_stats = db.get_daily_stats(yesterday)
    yesterday_chars = sum(s.total_chars for s in yesterday_stats)
    
    # 本周统计
    week_start = today - timedelta(days=today.weekday())
    week_data = db.get_recent_days_summary(7)
    week_chars = sum(d.get('total_chars', 0) for d in week_data)
    
    # 计算变化
    if yesterday_chars > 0:
        change_percent = ((today_chars - yesterday_chars) / yesterday_chars) * 100
    else:
        change_percent = 100 if today_chars > 0 else 0
    
    return {
        "today": {
            "chars": today_chars,
            "apps": len(today_stats),
            "change_percent": round(change_percent, 1),
        },
        "yesterday": {
            "chars": yesterday_chars,
            "apps": len(yesterday_stats),
        },
        "week": {
            "chars": week_chars,
            "avg_per_day": round(week_chars / max(len(week_data), 1)),
        }
    }


@app.get("/api/report/full")
def get_today_full_report():
    """获取今日完整报告"""
    return get_full_report(business_today().isoformat())


@app.get("/api/report/{target_date}", response_model=DailyReportResponse)
def get_daily_report(target_date: str):
    """获取指定日期的报告"""
    try:
        report_date = datetime.strptime(target_date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="日期格式错误，请使用 YYYY-MM-DD")
    
    analyzer = get_analyzer()
    report = analyzer.generate_daily_report(report_date)
    
    # 计算百分比
    total_chars = report.total_chars or 1
    app_stats = [
        AppStatsResponse(
            app_name=s.app_name,
            display_name=s.display_name,
            total_chars=s.total_chars,
            session_count=s.session_count,
            percentage=round(s.total_chars / total_chars * 100, 1),
        )
        for s in report.app_stats
    ]
    
    weekday_names = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']
    
    # 工作路径分析
    work_path_response = None
    if report.work_path:
        work_path_response = WorkPathAnalysisResponse(
            total_segments=report.work_path.total_segments,
            app_switches=report.work_path.app_switches,
            peak_hours=[{"hour": h, "chars": c} for h, c in report.work_path.peak_hours],
            focus_periods=[
                {
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "app": app,
                    "duration_minutes": round((end - start).total_seconds() / 60, 1)
                }
                for start, end, app in report.work_path.focus_periods
            ],
            work_pattern=report.work_path.work_pattern,
            segments=[
                WorkPathSegmentResponse(
                    start_time=seg.start_time.isoformat(),
                    end_time=seg.end_time.isoformat(),
                    app_name=seg.app_name,
                    display_name=seg.display_name,
                    char_count=seg.char_count,
                    duration_minutes=round(seg.duration_minutes, 1),
                    content_preview=seg.content_preview
                )
                for seg in report.work_path.segments[:50]  # 限制返回数量
            ] if len(report.work_path.segments) <= 50 else None
        )
    
    return DailyReportResponse(
        overview=DailyOverview(
            date=report_date.isoformat(),
            weekday=weekday_names[report_date.weekday()],
            total_chars=report.total_chars,
            total_apps=report.total_apps,
            total_sessions=report.total_sessions,
        ),
        app_stats=app_stats,
        main_activities=report.main_activities,
        summary=report.summary,
        suggestions=report.suggestions,
        work_path=work_path_response,
        ai_work_analysis=report.ai_work_analysis,
    )


@app.get("/api/report")
def get_today_report():
    """获取今日报告"""
    return get_daily_report(business_today().isoformat())


@app.get("/api/analysis/theme/{target_date}", response_model=Optional[ThemeAnalysisResponse])
def get_theme_analysis(target_date: str):
    """
    获取指定日期的主题深度分析
    
    分析用户全部输入内容，生成：
    - 今日主题列表
    - 工作重点回顾
    - 当前关注的内容
    - 洞察和启发
    - 详细总结
    """
    try:
        report_date = datetime.strptime(target_date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="日期格式错误，请使用 YYYY-MM-DD")
    
    analyzer = get_analyzer()
    theme = analyzer.generate_theme_analysis(report_date)
    
    if not theme:
        return None
    
    return ThemeAnalysisResponse(
        themes=theme.themes,
        work_focus=theme.work_focus,
        current_interests=theme.current_interests,
        insights=theme.insights,
        detailed_summary=theme.detailed_summary,
    )


@app.get("/api/analysis/theme")
def get_today_theme_analysis():
    """获取今日主题深度分析"""
    return get_theme_analysis(business_today().isoformat())


@app.get("/api/report/full/{target_date}", response_model=FullReportResponse)
def get_full_report(target_date: str):
    """
    获取完整报告（包含主题深度分析）
    
    包含：
    - 基础统计概览
    - 应用统计
    - 工作路径分析
    - AI 工作分析
    - 主题深度分析（今日主题、工作重点、关注内容、洞察启发）
    """
    try:
        report_date = datetime.strptime(target_date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="日期格式错误，请使用 YYYY-MM-DD")
    
    analyzer = get_analyzer()
    report = analyzer.generate_full_report(report_date)
    
    # 计算百分比
    total_chars = report.total_chars or 1
    app_stats = [
        AppStatsResponse(
            app_name=s.app_name,
            display_name=s.display_name,
            total_chars=s.total_chars,
            session_count=s.session_count,
            percentage=round(s.total_chars / total_chars * 100, 1),
        )
        for s in report.app_stats
    ]
    
    weekday_names = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']
    
    # 工作路径分析
    work_path_response = None
    if report.work_path:
        work_path_response = WorkPathAnalysisResponse(
            total_segments=report.work_path.total_segments,
            app_switches=report.work_path.app_switches,
            peak_hours=[{"hour": h, "chars": c} for h, c in report.work_path.peak_hours],
            focus_periods=[
                {
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "app": app,
                    "duration_minutes": round((end - start).total_seconds() / 60, 1)
                }
                for start, end, app in report.work_path.focus_periods
            ],
            work_pattern=report.work_path.work_pattern,
            segments=None
        )
    
    # 主题分析
    theme_response = None
    if report.theme_analysis:
        theme_response = ThemeAnalysisResponse(
            themes=report.theme_analysis.themes,
            work_focus=report.theme_analysis.work_focus,
            current_interests=report.theme_analysis.current_interests,
            insights=report.theme_analysis.insights,
            detailed_summary=report.theme_analysis.detailed_summary,
        )
    
    return FullReportResponse(
        overview=DailyOverview(
            date=report_date.isoformat(),
            weekday=weekday_names[report_date.weekday()],
            total_chars=report.total_chars,
            total_apps=report.total_apps,
            total_sessions=report.total_sessions,
        ),
        app_stats=app_stats,
        main_activities=report.main_activities,
        summary=report.summary,
        suggestions=report.suggestions,
        work_path=work_path_response,
        ai_work_analysis=report.ai_work_analysis,
        theme_analysis=theme_response,
    )


@app.get("/api/stats/hourly")
async def get_hourly_stats(target_date: Optional[str] = None):
    """获取每小时统计"""
    if target_date:
        try:
            report_date = datetime.strptime(target_date, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=400, detail="日期格式错误")
    else:
        report_date = business_today()
    
    db = get_database()
    records = db.get_records_by_date(report_date)
    
    # 按小时汇总
    hourly = {h: 0 for h in range(24)}
    for record in records:
        hour = record.timestamp.hour
        hourly[hour] += record.char_count
    
    return [HourlyStats(hour=h, chars=c) for h, c in hourly.items()]


@app.get("/api/stats/weekly")
async def get_weekly_stats():
    """获取最近7天统计"""
    db = get_database()
    days = db.get_recent_days_summary(7)
    
    weekday_names = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']
    
    result = []
    for day in days:
        d = datetime.strptime(day['day'], "%Y-%m-%d").date()
        result.append(WeeklyTrend(
            date=day['day'],
            weekday=weekday_names[d.weekday()],
            total_chars=day['total_chars'],
            app_count=day['app_count'],
        ))
    
    return result


@app.get("/api/stats/apps")
async def get_app_stats(
    target_date: Optional[str] = None,
    days: int = Query(default=1, ge=1, le=30)
):
    """获取应用统计"""
    db = get_database()
    
    if target_date:
        try:
            end_date = datetime.strptime(target_date, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=400, detail="日期格式错误")
    else:
        end_date = business_today()
    
    # 汇总多天数据
    all_stats = {}
    for i in range(days):
        d = end_date - timedelta(days=i)
        day_stats = db.get_daily_stats(d)
        for s in day_stats:
            if s.display_name not in all_stats:
                all_stats[s.display_name] = {
                    "app_name": s.app_name,
                    "display_name": s.display_name,
                    "total_chars": 0,
                    "session_count": 0,
                }
            all_stats[s.display_name]["total_chars"] += s.total_chars
            all_stats[s.display_name]["session_count"] += s.session_count
    
    # 排序并计算百分比
    total = sum(s["total_chars"] for s in all_stats.values()) or 1
    result = sorted(all_stats.values(), key=lambda x: x["total_chars"], reverse=True)
    
    for s in result:
        s["percentage"] = round(s["total_chars"] / total * 100, 1)
    
    return result


@app.get("/api/records")
async def get_records(
    target_date: Optional[str] = None,
    app: Optional[str] = None,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0)
):
    """获取输入记录列表"""
    db = get_database()
    
    if target_date:
        try:
            report_date = datetime.strptime(target_date, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=400, detail="日期格式错误")
    else:
        report_date = business_today()
    
    records = db.get_records_by_date(report_date)
    
    # 过滤应用
    if app:
        records = [r for r in records if r.display_name == app or r.app_name == app]
    
    # 分页
    total = len(records)
    records = records[offset:offset + limit]
    
    return {
        "total": total,
        "records": [
            RecordItem(
                id=r.id,
                timestamp=r.timestamp.isoformat(),
                app_name=r.app_name,
                display_name=r.display_name,
                content=r.content[:200] if r.content else "",  # 限制长度
                char_count=r.char_count,
            )
            for r in records
        ]
    }


@app.get("/api/apps")
async def get_app_list():
    """获取所有应用列表"""
    db = get_database()
    
    # 获取最近30天的应用
    apps = set()
    for i in range(30):
        d = business_today() - timedelta(days=i)
        stats = db.get_daily_stats(d)
        for s in stats:
            apps.add((s.app_name, s.display_name))
    
    return [{"app_name": a, "display_name": d} for a, d in sorted(apps, key=lambda x: x[1])]


@app.post("/api/export/obsidian/{target_date}")
async def export_to_obsidian(
    target_date: str,
    include_raw: bool = True,
    include_ai: bool = True
):
    """
    导出日报到 Obsidian
    
    将指定日期的输入记录和 AI 分析导出为 Markdown 文件保存到 Obsidian
    """
    try:
        report_date = datetime.strptime(target_date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="日期格式错误，请使用 YYYY-MM-DD")
    
    filepath = export_daily_to_obsidian(
        target_date=report_date,
        include_raw_content=include_raw,
        include_ai_analysis=include_ai
    )
    
    if filepath:
        return {
            "success": True,
            "message": f"已导出到 {filepath}",
            "filepath": str(filepath)
        }
    else:
        return {
            "success": False,
            "message": f"{report_date} 没有输入记录",
            "filepath": None
        }


@app.post("/api/export/obsidian")
async def export_today_to_obsidian(include_raw: bool = True, include_ai: bool = True):
    """导出今日日报到 Obsidian"""
    return await export_to_obsidian(business_today().isoformat(), include_raw, include_ai)


@app.get("/api/content/by-app")
async def get_content_by_app(target_date: Optional[str] = None):
    """按应用返回每次输入的独立记录，不将不同提交拼成一句话。"""
    db = get_database()

    if target_date:
        try:
            report_date = datetime.strptime(target_date, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=400, detail="日期格式错误")
    else:
        report_date = business_today()

    records = db.get_records_by_date(report_date)
    app_contents = {}
    for record in records:
        # 同名应用可能有不同 bundle ID，避免归并到同一组。
        key = record.app_bundle_id or record.app_name or record.display_name
        if key not in app_contents:
            app_contents[key] = {
                "app_name": record.app_name,
                "app_bundle_id": record.app_bundle_id,
                "display_name": record.display_name,
                "total_chars": 0,
                "recorded_text_chars": 0,
                "estimated_key_count": 0,
                "sessions": [],
            }

        group = app_contents[key]
        content = record.content or ""
        has_text = bool(content)
        count_only = not has_text and record.char_count > 0
        group["total_chars"] += record.char_count
        if has_text:
            group["recorded_text_chars"] += len(content)
        if count_only:
            group["estimated_key_count"] += record.char_count
        group["sessions"].append({
            "id": record.id,
            "timestamp": record.timestamp.isoformat(),
            "time": record.timestamp.strftime("%H:%M:%S"),
            "content": content,
            "char_count": record.char_count,
            "count_only": count_only,
        })

    result = []
    for group in sorted(app_contents.values(), key=lambda entry: -entry["total_chars"]):
        # 保留旧响应字段供已有客户端读取，使用段落边界而非中文直接粘连。
        group["full_content"] = "\n\n".join(
            row["content"] for row in group["sessions"] if row["content"]
        )
        group["session_count"] = len(group["sessions"])
        result.append(group)

    return result


# ===== 静态文件和首页 =====

# 获取 web 目录路径
WEB_DIR = Path(__file__).parent


@app.get("/", response_class=HTMLResponse)
async def index():
    """首页"""
    html_path = WEB_DIR / "templates" / "index.html"
    if html_path.exists():
        return FileResponse(html_path)
    return HTMLResponse("<h1>OmniMe Web Dashboard</h1><p>请确保 templates/index.html 存在</p>")


# 挂载静态文件
static_dir = WEB_DIR / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
