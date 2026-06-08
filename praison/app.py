"""FastAPI web app: Praise-credential login, then per-user month view."""

import logging
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from praison.calculator import calculate_month_stats, merge_actual_and_planned
from praison.crypto import decrypt, encrypt, session_secret
from praison.database import Store, create_database
from praison.duration import JST, Duration
from praison.errors import InvalidPraiseLoginError
from praison.models import DayRecord, DayType, MonthStats, PlannedDay, ServerSummary, User
from praison.parser import (
    build_location_categories,
    parse_summary,
    parse_timesheet,
)
from praison.praise.session import PraiseSession, normalize_url, verify_credentials

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 600  # auto refresh praise data every 10 minutes

_TEMPLATES_DIR = Path(__file__).parent / "templates"


@dataclass
class CachedMonth:
    records: list[DayRecord]
    summary: ServerSummary
    fetched_at: float


class _NotAuthenticatedError(Exception):
    """Raised by the auth dependency when there is no valid session."""


class PraiseCache:
    """Per-user server-side cache of Praise timesheets so page loads never block."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._months: dict[tuple[str, int, int], CachedMonth] = {}
        self._location_categories: dict[str, dict[str, str]] = {}
        self._last_error: dict[str, str | None] = {}

    def last_error(self, user_id: str) -> str | None:
        return self._last_error.get(user_id)

    def get_month(
        self, user: User, year: int, month: int, *, force: bool = False
    ) -> CachedMonth | None:
        key = (user.id, year, month)
        with self._lock:
            cached = self._months.get(key)
            if cached and not force and time.time() - cached.fetched_at < CACHE_TTL_SECONDS:
                return cached
        try:
            fresh = self._fetch(user, year, month)
        except Exception as exc:  # noqa: BLE001 - praise being down must not kill the page
            logger.warning("praise fetch failed for %s: %s", user.id, exc)
            self._last_error[user.id] = str(exc)
            return cached
        with self._lock:
            self._months[key] = fresh
            self._last_error[user.id] = None
        return fresh

    def _fetch(self, user: User, year: int, month: int) -> CachedMonth:
        password = decrypt(user.encrypted_password)
        with PraiseSession(
            user.praise_url, user.praise_email, password, session_path=None
        ) as praise:
            if user.id not in self._location_categories:
                try:
                    self._location_categories[user.id] = build_location_categories(
                        praise.get_locations()
                    )
                except Exception as exc:  # noqa: BLE001 - fall back to name heuristics
                    logger.warning("could not fetch locations, using name heuristics: %s", exc)
                    self._location_categories[user.id] = {}
            data = praise.get_timesheet(year, month)
        records = parse_timesheet(
            data,
            location_categories=self._location_categories[user.id],
            hours_per_day=user.hours_per_day,
        )
        return CachedMonth(records=records, summary=parse_summary(data), fetched_at=time.time())


def _month_nav(year: int, month: int) -> dict[str, Any]:
    prev_y, prev_m = (year - 1, 12) if month == 1 else (year, month - 1)
    next_y, next_m = (year + 1, 1) if month == 12 else (year, month + 1)
    return {"prev": (prev_y, prev_m), "next": (next_y, next_m)}


def create_app(db: Store | None = None) -> FastAPI:
    app = FastAPI(title="praison")
    app.add_middleware(SessionMiddleware, secret_key=session_secret(), same_site="lax")
    db = db or create_database()
    cache = PraiseCache()
    templates = Jinja2Templates(directory=_TEMPLATES_DIR)
    templates.env.filters["dur"] = lambda minutes: str(Duration(int(minutes)))
    templates.env.filters["hours"] = lambda h: str(Duration(round(h * 60)))

    app.mount("/static", StaticFiles(directory=_TEMPLATES_DIR / "static"), name="static")

    def require_user(request: Request) -> User:
        user_id = request.session.get("user_id")
        if user_id:
            user = db.get_user_by_id(user_id)
            if user:
                return user
        raise _NotAuthenticatedError

    @app.exception_handler(_NotAuthenticatedError)
    async def _redirect_to_login(request: Request, exc: _NotAuthenticatedError) -> Response:  # noqa: ARG001
        # HTMX swaps wouldn't follow a 303, so ask it to do a full-page redirect.
        if request.headers.get("HX-Request"):
            return Response(status_code=204, headers={"HX-Redirect": "/login"})
        return RedirectResponse("/login", status_code=303)

    def month_context(
        request: Request, user: User, year: int, month: int, *, force: bool = False
    ) -> dict[str, Any]:
        today = datetime.now(JST).date()
        cached = cache.get_month(user, year, month, force=force)
        actual_records = cached.records if cached else []
        summary = cached.summary if cached else None
        planned = db.get_planned_days_for_month(user.id, year, month)
        merged = merge_actual_and_planned(
            actual_records,
            planned,
            year,
            month,
            today=today,
            hours_per_day=user.hours_per_day,
        )
        stats, daily_balances = calculate_month_stats(
            year,
            month,
            merged,
            today=today,
            hours_per_day=user.hours_per_day,
            wfh_hours_per_day=user.wfh_hours_per_business_day,
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
            "fetch_error": cache.last_error(user.id),
            "fetched_at": (
                datetime.fromtimestamp(cached.fetched_at, tz=JST).strftime("%H:%M")
                if cached
                else None
            ),
            "DayType": DayType,
            "MonthStats": MonthStats,
        }

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request) -> Response:
        if request.session.get("user_id"):
            return RedirectResponse("/", status_code=303)
        return templates.TemplateResponse(request, "login.html", {"request": request})

    @app.post("/login", response_class=HTMLResponse)
    def login_submit(
        request: Request,
        praise_url: Annotated[str, Form()],
        email: Annotated[str, Form()],
        password: Annotated[str, Form()],
    ) -> Response:
        url = normalize_url(praise_url)
        email = email.strip()
        try:
            verify_credentials(url, email, password)
        except InvalidPraiseLoginError:
            return _login_error(request, "Praise rejected those credentials.")
        except Exception as exc:  # noqa: BLE001 - surface connection issues to the user
            logger.warning("login verification failed: %s", exc)
            return _login_error(request, "Could not reach that Praise server.")

        encrypted = encrypt(password)
        user = db.get_user_by_identity(url, email)
        if user is None:
            user = db.create_user(
                url, email, encrypted, hours_per_day=8, wfh_hours_per_business_day=1.5
            )
        else:
            db.update_login(user.id, encrypted)
        request.session["user_id"] = user.id
        return RedirectResponse("/", status_code=303)

    def _login_error(request: Request, message: str) -> HTMLResponse:
        return templates.TemplateResponse(
            request, "login.html", {"request": request, "error": message}, status_code=401
        )

    @app.post("/logout")
    def logout(request: Request) -> RedirectResponse:
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    @app.get("/", response_class=HTMLResponse)
    def index(user: Annotated[User, Depends(require_user)]) -> RedirectResponse:  # noqa: ARG001
        now = datetime.now(JST)
        return RedirectResponse(f"/month/{now.year}/{now.month}")

    @app.get("/month/{year}/{month}", response_class=HTMLResponse)
    def month_view(
        request: Request, user: Annotated[User, Depends(require_user)], year: int, month: int
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request, "month.html", month_context(request, user, year, month)
        )

    @app.get("/month/{year}/{month}/content", response_class=HTMLResponse)
    def month_content(
        request: Request, user: Annotated[User, Depends(require_user)], year: int, month: int
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request, "_content.html", month_context(request, user, year, month)
        )

    @app.post("/month/{year}/{month}/refresh", response_class=HTMLResponse)
    def month_refresh(
        request: Request, user: Annotated[User, Depends(require_user)], year: int, month: int
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request, "_content.html", month_context(request, user, year, month, force=True)
        )

    @app.get("/plan/{day}", response_class=HTMLResponse)
    def plan_form(
        request: Request, user: Annotated[User, Depends(require_user)], day: str
    ) -> HTMLResponse:
        target = date.fromisoformat(day)
        planned = db.get_planned_day(user.id, target)
        return templates.TemplateResponse(
            request,
            "_plan_form.html",
            {"day": target, "planned": planned},
        )

    @app.post("/plan/{day}", response_class=HTMLResponse)
    def plan_save(
        request: Request,
        user: Annotated[User, Depends(require_user)],
        day: str,
        office_hours: Annotated[str, Form()] = "0",
        wfh_hours: Annotated[str, Form()] = "0",
        leave: Annotated[str, Form()] = "none",
        note: Annotated[str, Form()] = "",
    ) -> HTMLResponse:
        target = date.fromisoformat(day)
        db.save_planned_day(
            user.id,
            PlannedDay(
                date=target,
                office_minutes=_parse_hours_to_minutes(office_hours),
                remote_minutes=_parse_hours_to_minutes(wfh_hours),
                is_paid_leave=leave in ("full", "half"),
                is_half_day_leave=leave == "half",
                note=note.strip(),
            ),
        )
        return templates.TemplateResponse(
            request, "_content.html", month_context(request, user, target.year, target.month)
        )

    @app.delete("/plan/{day}", response_class=HTMLResponse)
    def plan_delete(
        request: Request, user: Annotated[User, Depends(require_user)], day: str
    ) -> HTMLResponse:
        target = date.fromisoformat(day)
        db.delete_planned_day(user.id, target)
        return templates.TemplateResponse(
            request, "_content.html", month_context(request, user, target.year, target.month)
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
