"""FastAPI web app: month view with planning and live stats."""

import logging
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from praison.calculator import calculate_month_stats, merge_actual_and_planned
from praison.config import Config
from praison.database import PlanningStore, create_database
from praison.duration import JST, Duration
from praison.models import DayRecord, DayType, MonthStats, PlannedDay, ServerSummary
from praison.parser import (
    build_location_categories,
    parse_summary,
    parse_timesheet,
)
from praison.praise.session import PraiseSession

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 600  # auto refresh praise data every 10 minutes

_TEMPLATES_DIR = Path(__file__).parent / "templates"


@dataclass
class CachedMonth:
    records: list[DayRecord]
    summary: ServerSummary
    fetched_at: float


class PraiseCache:
    """Server-side cache of praise timesheets so page loads never block."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._lock = threading.Lock()
        self._months: dict[tuple[int, int], CachedMonth] = {}
        self._location_categories: dict[str, str] | None = None
        self._last_error: str | None = None

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def get_month(self, year: int, month: int, *, force: bool = False) -> CachedMonth | None:
        with self._lock:
            cached = self._months.get((year, month))
            if cached and not force and time.time() - cached.fetched_at < CACHE_TTL_SECONDS:
                return cached
        try:
            fresh = self._fetch(year, month)
        except Exception as exc:  # noqa: BLE001 - praise being down must not kill the page
            logger.warning("praise fetch failed: %s", exc)
            self._last_error = str(exc)
            return cached
        with self._lock:
            self._months[(year, month)] = fresh
            self._last_error = None
        return fresh

    def _fetch(self, year: int, month: int) -> CachedMonth:
        with PraiseSession(
            self._config.praise_url, self._config.praise_email, self._config.praise_password
        ) as praise:
            if self._location_categories is None:
                try:
                    self._location_categories = build_location_categories(praise.get_locations())
                except Exception as exc:  # noqa: BLE001 - fall back to name heuristics
                    logger.warning("could not fetch locations, using name heuristics: %s", exc)
                    self._location_categories = {}
            data = praise.get_timesheet(year, month)
        records = parse_timesheet(
            data,
            location_categories=self._location_categories,
            hours_per_day=self._config.hours_per_day,
        )
        return CachedMonth(records=records, summary=parse_summary(data), fetched_at=time.time())


def _month_nav(year: int, month: int) -> dict[str, Any]:
    prev_y, prev_m = (year - 1, 12) if month == 1 else (year, month - 1)
    next_y, next_m = (year + 1, 1) if month == 12 else (year, month + 1)
    return {"prev": (prev_y, prev_m), "next": (next_y, next_m)}


def create_app(config: Config, db: PlanningStore | None = None) -> FastAPI:
    app = FastAPI(title="praison")
    db = db or create_database()
    cache = PraiseCache(config)
    templates = Jinja2Templates(directory=_TEMPLATES_DIR)
    templates.env.filters["dur"] = lambda minutes: str(Duration(int(minutes)))
    templates.env.filters["hours"] = lambda h: str(Duration(round(h * 60)))

    app.mount("/static", StaticFiles(directory=_TEMPLATES_DIR / "static"), name="static")

    def month_context(
        request: Request, year: int, month: int, *, force: bool = False
    ) -> dict[str, Any]:
        today = datetime.now(JST).date()
        cached = cache.get_month(year, month, force=force)
        actual_records = cached.records if cached else []
        summary = cached.summary if cached else None
        planned = db.get_planned_days_for_month(year, month)
        merged = merge_actual_and_planned(
            actual_records,
            planned,
            year,
            month,
            today=today,
            hours_per_day=config.hours_per_day,
        )
        stats, daily_balances = calculate_month_stats(
            year,
            month,
            merged,
            today=today,
            hours_per_day=config.hours_per_day,
            wfh_hours_per_day=config.wfh_hours_per_business_day,
            server_summary=summary,
        )
        planned_dates = {p.date for p in planned}
        return {
            "request": request,
            "year": year,
            "month": month,
            "today": today,
            "records": merged,
            "stats": stats,
            "daily_balances": daily_balances,
            "planned_dates": planned_dates,
            "summary": summary,
            "nav": _month_nav(year, month),
            "fetch_error": cache.last_error,
            "fetched_at": (
                datetime.fromtimestamp(cached.fetched_at, tz=JST).strftime("%H:%M")
                if cached
                else None
            ),
            "DayType": DayType,
            "MonthStats": MonthStats,
        }

    @app.get("/", response_class=HTMLResponse)
    def index() -> RedirectResponse:
        now = datetime.now(JST)
        return RedirectResponse(f"/month/{now.year}/{now.month}")

    @app.get("/month/{year}/{month}", response_class=HTMLResponse)
    def month_view(request: Request, year: int, month: int) -> HTMLResponse:
        return templates.TemplateResponse(
            request, "month.html", month_context(request, year, month)
        )

    @app.get("/month/{year}/{month}/content", response_class=HTMLResponse)
    def month_content(request: Request, year: int, month: int) -> HTMLResponse:
        return templates.TemplateResponse(
            request, "_content.html", month_context(request, year, month)
        )

    @app.post("/month/{year}/{month}/refresh", response_class=HTMLResponse)
    def month_refresh(request: Request, year: int, month: int) -> HTMLResponse:
        return templates.TemplateResponse(
            request, "_content.html", month_context(request, year, month, force=True)
        )

    @app.get("/plan/{day}", response_class=HTMLResponse)
    def plan_form(request: Request, day: str) -> HTMLResponse:
        target = date.fromisoformat(day)
        planned = db.get_planned_day(target)
        return templates.TemplateResponse(
            request,
            "_plan_form.html",
            {"day": target, "planned": planned},
        )

    @app.post("/plan/{day}", response_class=HTMLResponse)
    def plan_save(
        request: Request,
        day: str,
        office_hours: Annotated[str, Form()] = "0",
        wfh_hours: Annotated[str, Form()] = "0",
        leave: Annotated[str, Form()] = "none",
        note: Annotated[str, Form()] = "",
    ) -> HTMLResponse:
        target = date.fromisoformat(day)
        db.save_planned_day(
            PlannedDay(
                date=target,
                office_minutes=_parse_hours_to_minutes(office_hours),
                remote_minutes=_parse_hours_to_minutes(wfh_hours),
                is_paid_leave=leave in ("full", "half"),
                is_half_day_leave=leave == "half",
                note=note.strip(),
            )
        )
        return templates.TemplateResponse(
            request, "_content.html", month_context(request, target.year, target.month)
        )

    @app.delete("/plan/{day}", response_class=HTMLResponse)
    def plan_delete(request: Request, day: str) -> HTMLResponse:
        target = date.fromisoformat(day)
        db.delete_planned_day(target)
        return templates.TemplateResponse(
            request, "_content.html", month_context(request, target.year, target.month)
        )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


def _parse_hours_to_minutes(value: str) -> int:
    """Accept '8', '7.5' or '7:30' style input."""
    value = value.strip()
    if not value:
        return 0
    if ":" in value:
        return Duration.parse(value).minutes
    return round(float(value) * 60)
