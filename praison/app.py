"""FastAPI web app: Praise device-flow login, then per-user month view."""

import contextlib
import logging
import os
import threading
import time
from collections import defaultdict, deque
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
from praison.errors import (
    PraiseCliLoginError,
    PraiseTokenExpiredError,
    PraiseUrlNotAllowedError,
)
from praison.models import DayRecord, DayType, MonthStats, PlannedDay, ServerSummary, User
from praison.parser import (
    build_location_categories,
    parse_summary,
    parse_timesheet,
)
from praison.praise.session import (
    PraiseSession,
    fetch_me,
    logout_token,
    normalize_url,
    poll_cli_token,
    start_cli_login,
)
from praison.security import assert_praise_url_allowed

logger = logging.getLogger(__name__)


def _app_version() -> str:
    """Installed package version, shown on the page so the running build is visible."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("praison")
    except PackageNotFoundError:
        return "dev"


CACHE_TTL_SECONDS = 600  # auto refresh praise data every 10 minutes
REFRESH_MIN_INTERVAL_SECONDS = 60  # ignore manual refreshes more frequent than this

# Defaults for a newly registered user; both are editable per user via /settings.
DEFAULT_HOURS_PER_DAY = 8
DEFAULT_WFH_PER_BUSINESS_DAY = 1.0

_TEMPLATES_DIR = Path(__file__).parent / "templates"

# Login throttling: at most this many attempts per client IP per window.
LOGIN_MAX_ATTEMPTS = int(os.environ.get("PRAISON_LOGIN_MAX_ATTEMPTS", "10"))
LOGIN_WINDOW_SECONDS = int(os.environ.get("PRAISON_LOGIN_WINDOW_SECONDS", "300"))

# All scripts/styles are served from this origin (htmx is vendored locally and
# there are no inline scripts), so the policy can stay tight. Inline styles are
# tolerated; they cannot exfiltrate credentials the way injected script can.
_CSP = (
    "default-src 'self'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'; "
    "img-src 'self' data:; "
    "style-src 'self' 'unsafe-inline'; "
    "script-src 'self'; "
    "connect-src 'self'; "
    "object-src 'none'"
)


def _https_only() -> bool:
    """Whether session cookies get the Secure flag. On by default; set
    ``PRAISON_HTTPS_ONLY=false`` for plain-HTTP local runs (TLS terminates at the
    proxy in production, so the app itself sees HTTP either way)."""
    return os.environ.get("PRAISON_HTTPS_ONLY", "true").strip().lower() not in (
        "false",
        "0",
        "no",
    )


class _LoginRateLimiter:
    """Fixed-window per-IP throttle for the login endpoint."""

    def __init__(self, max_attempts: int, window_seconds: int) -> None:
        self._max = max_attempts
        self._window = window_seconds
        self._lock = threading.Lock()
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, client: str) -> bool:
        now = time.time()
        with self._lock:
            hits = self._hits[client]
            while hits and now - hits[0] > self._window:
                hits.popleft()
            if len(hits) >= self._max:
                return False
            hits.append(now)
            return True


@dataclass
class CachedMonth:
    records: list[DayRecord]
    summary: ServerSummary
    fetched_at: float


class _NotAuthenticatedError(Exception):
    """Raised by the auth dependency when there is no valid session."""


class PraiseCache:
    """Per-user server-side cache of Praise timesheets so page loads never block."""

    def __init__(self, db: Store) -> None:
        self._db = db
        self._lock = threading.Lock()
        self._months: dict[tuple[str, int, int], CachedMonth] = {}
        self._location_categories: dict[str, dict[str, str]] = {}
        self._last_error: dict[str, str | None] = {}

    def last_error(self, user_id: str) -> str | None:
        return self._last_error.get(user_id)

    def needs_reauth(self, user_id: str) -> bool:
        """True when the user has no usable Praise token (must re-run the flow)."""
        return self._db.get_praise_token(user_id) is None

    def clear_user(self, user_id: str) -> None:
        """Drop a user's cached months and Praise token (called on logout)."""
        with self._lock:
            self._months = {k: v for k, v in self._months.items() if k[0] != user_id}

    def _drop_token(self, user_id: str) -> None:
        """Forget a user's token so the UI prompts them to re-authorize."""
        with contextlib.suppress(Exception):
            self._db.delete_praise_token(user_id)

    def get_month(
        self, user: User, year: int, month: int, *, force: bool = False
    ) -> CachedMonth | None:
        key = (user.id, year, month)
        with self._lock:
            cached = self._months.get(key)
            if cached and not force and time.time() - cached.fetched_at < CACHE_TTL_SECONDS:
                return cached
            # Rate-limit manual refreshes: a forced refetch within the minimum
            # interval just returns the existing data.
            if cached and force and time.time() - cached.fetched_at < REFRESH_MIN_INTERVAL_SECONDS:
                return cached
        try:
            fresh = self._fetch(user, year, month)
        except PraiseTokenExpiredError as exc:
            # The token was rejected (expired/revoked/evicted). Forget it so the
            # page surfaces a re-authorize prompt instead of looping.
            logger.info("praise token expired for %s: %s", user.id, exc)
            self._drop_token(user.id)
            self._last_error[user.id] = None
            return cached
        except Exception as exc:  # noqa: BLE001 - praise being down must not kill the page
            logger.warning("praise fetch failed for %s: %s", user.id, exc)
            self._last_error[user.id] = str(exc)
            return cached
        with self._lock:
            self._months[key] = fresh
            self._last_error[user.id] = None
        return fresh

    def _fetch(self, user: User, year: int, month: int) -> CachedMonth:
        enc = self._db.get_praise_token(user.id)
        if not enc:
            raise PraiseTokenExpiredError("no Praise token stored")
        try:
            token = decrypt(enc)
        except Exception as exc:
            raise PraiseTokenExpiredError("stored token could not be decrypted") from exc
        with PraiseSession(user.praise_url, token) as praise:
            if user.id not in self._location_categories:
                try:
                    self._location_categories[user.id] = build_location_categories(
                        praise.get_locations()
                    )
                except PraiseTokenExpiredError:
                    raise
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
    app.add_middleware(
        SessionMiddleware,
        secret_key=session_secret(),
        same_site="lax",
        https_only=_https_only(),
    )

    @app.middleware("http")
    async def _add_security_headers(request: Request, call_next: Any) -> Response:
        response: Response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", _CSP)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=63072000; includeSubDomains"
        )
        return response

    db = db or create_database()
    cache = PraiseCache(db)
    login_limiter = _LoginRateLimiter(LOGIN_MAX_ATTEMPTS, LOGIN_WINDOW_SECONDS)
    templates = Jinja2Templates(directory=_TEMPLATES_DIR)
    templates.env.filters["dur"] = lambda minutes: str(Duration(int(minutes)))
    templates.env.filters["hours"] = lambda h: str(Duration(round(h * 60)))
    templates.env.globals["app_version"] = _app_version()

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
        request: Request,
        user: User,
        year: int,
        month: int,
        *,
        force: bool = False,
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
            planned_days=planned,
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
            "needs_reauth": cache.needs_reauth(user.id),
            "fetch_error": cache.last_error(user.id),
            "fetched_at": (
                datetime.fromtimestamp(cached.fetched_at, tz=JST).strftime("%H:%M")
                if cached
                else None
            ),
            "DayType": DayType,
            "MonthStats": MonthStats,
        }

    def _has_valid_token(request: Request) -> bool:
        user_id = request.session.get("user_id")
        return user_id is not None and db.get_praise_token(user_id) is not None

    def _login_card(
        request: Request, *, error: str | None = None, status: int = 200
    ) -> HTMLResponse:
        """Render the login card (the URL form). A fragment for HTMX, else a page."""
        prefill_url = ""
        reauth = False
        user_id = request.session.get("user_id")
        if user_id:
            existing = db.get_user_by_id(user_id)
            if existing:
                prefill_url = existing.praise_url
                reauth = True
        template = "_login_card.html" if request.headers.get("HX-Request") else "login.html"
        return templates.TemplateResponse(
            request,
            template,
            {"request": request, "error": error, "reauth": reauth, "prefill_url": prefill_url},
            status_code=status,
        )

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request) -> Response:
        # Skip the form only when the user is signed in AND has a usable token.
        if _has_valid_token(request):
            return RedirectResponse("/", status_code=303)
        return _login_card(request)

    @app.post("/login/start", response_class=HTMLResponse)
    def login_start(
        request: Request,
        praise_url: Annotated[str, Form()],
    ) -> Response:
        client = request.client.host if request.client else "unknown"
        if not login_limiter.allow(client):
            return _login_card(
                request, error="Too many attempts. Please wait and try again.", status=429
            )
        url = normalize_url(praise_url)
        try:
            assert_praise_url_allowed(url)
        except PraiseUrlNotAllowedError:
            return _login_card(request, error="That Praise server is not permitted here.")
        try:
            start = start_cli_login(url)
        except Exception as exc:  # noqa: BLE001 - surface connection issues to the user
            logger.warning("cli/start failed for %s: %s", url, exc)
            return _login_card(request, error="Could not reach that Praise server.")

        request.session["pending_login"] = {
            "base_url": start.base_url,
            "device_code": start.device_code,
            "expires_at": start.expires_at,
        }
        user_code = start.user_code
        formatted = f"{user_code[:4]}-{user_code[4:]}" if len(user_code) >= 8 else user_code
        return templates.TemplateResponse(
            request,
            "_login_waiting.html",
            {
                "request": request,
                "verification_url": start.verification_url,
                "user_code": formatted,
                "interval": start.interval_seconds,
            },
        )

    @app.get("/login/poll", response_class=HTMLResponse)
    def login_poll(request: Request) -> Response:
        pending = request.session.get("pending_login")
        if not pending:
            return Response(status_code=204, headers={"HX-Redirect": "/login"})
        base_url = pending["base_url"]
        try:
            token = poll_cli_token(base_url, pending["device_code"])
        except PraiseCliLoginError as exc:
            request.session.pop("pending_login", None)
            return _login_card(request, error=str(exc))
        except Exception as exc:  # noqa: BLE001 - transient; let the poller keep trying
            logger.info("cli/token poll error (will retry): %s", exc)
            return Response(status_code=204)
        if token is None:
            return Response(status_code=204)  # still pending; HTMX keeps polling

        try:
            me = fetch_me(base_url, token)
            email = str(me["email"]).strip()
        except Exception as exc:  # noqa: BLE001 - couldn't confirm identity
            logger.warning("fetch_me failed after token grant: %s", exc)
            request.session.pop("pending_login", None)
            return _login_card(request, error="Praise authorization failed. Please try again.")

        user = db.get_user_by_identity(base_url, email)
        if user is None:
            user = db.create_user(
                base_url,
                email,
                hours_per_day=DEFAULT_HOURS_PER_DAY,
                wfh_hours_per_business_day=DEFAULT_WFH_PER_BUSINESS_DAY,
            )
        else:
            db.update_login(user.id)
        db.save_praise_token(user.id, encrypt(token))
        request.session.pop("pending_login", None)
        request.session["user_id"] = user.id
        return Response(status_code=204, headers={"HX-Redirect": "/"})

    @app.post("/logout")
    def logout(request: Request) -> RedirectResponse:
        user_id = request.session.get("user_id")
        if user_id:
            # Best-effort revoke the Praise token, then forget it locally.
            enc = db.get_praise_token(user_id)
            if enc:
                user = db.get_user_by_id(user_id)
                with contextlib.suppress(Exception):
                    if user:
                        logout_token(user.praise_url, decrypt(enc))
                db.delete_praise_token(user_id)
            cache.clear_user(user_id)
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    @app.get("/settings", response_class=HTMLResponse)
    def settings_form(
        request: Request,
        user: Annotated[User, Depends(require_user)],
        year: int | None = None,
        month: int | None = None,
    ) -> HTMLResponse:
        now = datetime.now(JST)
        return templates.TemplateResponse(
            request,
            "_settings_form.html",
            {
                "request": request,
                "user": user,
                "year": year or now.year,
                "month": month or now.month,
            },
        )

    @app.post("/settings", response_class=HTMLResponse)
    def settings_save(
        request: Request,
        user: Annotated[User, Depends(require_user)],
        hours_per_day: Annotated[str, Form()],
        wfh_hours_per_business_day: Annotated[str, Form()],
        year: Annotated[int, Form()],
        month: Annotated[int, Form()],
    ) -> Response:
        try:
            hours = int(float(hours_per_day))
            wfh = float(wfh_hours_per_business_day)
        except ValueError:
            # Re-render the modal (not #content) so the error stays in the dialog.
            return templates.TemplateResponse(
                request,
                "_settings_form.html",
                {
                    "request": request,
                    "user": user,
                    "year": year,
                    "month": month,
                    "error": "Please enter valid numbers.",
                },
                status_code=400,
                headers={"HX-Retarget": "#modal", "HX-Reswap": "innerHTML"},
            )
        db.update_settings(user.id, hours, wfh)
        # Settings affect the month calculations, so re-render the current month.
        user = db.get_user_by_id(user.id) or user
        return templates.TemplateResponse(
            request,
            "_content.html",
            month_context(request, user, year, month),
        )

    @app.get("/", response_class=HTMLResponse)
    def index(user: Annotated[User, Depends(require_user)]) -> RedirectResponse:  # noqa: ARG001
        now = datetime.now(JST)
        return RedirectResponse(f"/month/{now.year}/{now.month}")

    @app.get("/month/{year}/{month}", response_class=HTMLResponse)
    def month_view(
        request: Request,
        user: Annotated[User, Depends(require_user)],
        year: int,
        month: int,
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request, "month.html", month_context(request, user, year, month)
        )

    @app.get("/month/{year}/{month}/content", response_class=HTMLResponse)
    def month_content(
        request: Request,
        user: Annotated[User, Depends(require_user)],
        year: int,
        month: int,
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request, "_content.html", month_context(request, user, year, month)
        )

    @app.post("/month/{year}/{month}/refresh", response_class=HTMLResponse)
    def month_refresh(
        request: Request,
        user: Annotated[User, Depends(require_user)],
        year: int,
        month: int,
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "_content.html",
            month_context(request, user, year, month, force=True),
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
        # Full-day and unpaid leave are whole days off, so worked hours are zeroed.
        # Half-day leave keeps any hours entered for the half that is worked.
        on_leave = leave in ("full", "unpaid")
        db.save_planned_day(
            user.id,
            PlannedDay(
                date=target,
                office_minutes=0 if on_leave else _parse_hours_to_minutes(office_hours),
                remote_minutes=0 if on_leave else _parse_hours_to_minutes(wfh_hours),
                is_paid_leave=leave in ("full", "half"),
                is_half_day_leave=leave == "half",
                is_unpaid_leave=leave == "unpaid",
                note=note.strip(),
            ),
        )
        return templates.TemplateResponse(
            request,
            "_content.html",
            month_context(request, user, target.year, target.month),
        )

    @app.delete("/plan/{day}", response_class=HTMLResponse)
    def plan_delete(
        request: Request,
        user: Annotated[User, Depends(require_user)],
        day: str,
    ) -> HTMLResponse:
        target = date.fromisoformat(day)
        db.delete_planned_day(user.id, target)
        return templates.TemplateResponse(
            request,
            "_content.html",
            month_context(request, user, target.year, target.month),
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
