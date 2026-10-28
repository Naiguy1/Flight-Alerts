"""Authenticated, read-mostly admin UI. Work is persisted for the worker to execute."""

import asyncio
import base64
import copy
import hashlib
import hmac
import logging
import re
import time
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, FastAPI, Form, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.concurrency import run_in_threadpool

from web.security import CSRFSigner, LoginLimiter, canonical_https_origin, valid_password_hash, verify_password

logger = logging.getLogger("flight_alerts.web")
MAX_BODY_BYTES = 64 * 1024
MAX_BODY_CHUNKS = 1024
BODY_READ_SECONDS = 10
TEMPLATES = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "web" / "templates"),
    autoescape=select_autoescape(["html"], default_for_string=True, default=True),
)
CSS = """
:root {font-family:system-ui,sans-serif;color:#132b40;background:#f1f5f9;color-scheme:light}
body {max-width:1200px;margin:auto;padding:24px} header,nav,.row{display:flex;gap:14px;align-items:center;flex-wrap:wrap}
header{justify-content:space-between}nav{margin:20px 0}a{color:#075da8}section{background:white;border:1px solid #d9e2eb;border-radius:10px;padding:20px;margin:18px 0;overflow:auto}
h1{margin-bottom:4px}h2{font-size:1.2rem}small,.muted{color:#526878}.notice{padding:14px;background:#e3f0fb;border-radius:8px}
label{display:inline-flex;flex-direction:column;gap:5px;margin:6px 10px 8px 0}input,select,button{font:inherit;padding:9px;border:1px solid #aebdcb;border-radius:5px}
button{background:#075da8;color:white;cursor:pointer}button.danger{background:#9d2222}table{width:100%;border-collapse:collapse}td,th{padding:10px;border-bottom:1px solid #e0e7ee;text-align:left}
form{display:inline}fieldset{border:1px solid #d9e2eb;margin:14px 0}.check{flex-direction:row;align-items:center}code{overflow-wrap:anywhere}.status{font-weight:bold}
"""


class SecurityMiddleware:
    """Bound streamed request bodies and apply response controls before application code."""

    def __init__(self, app, owner):
        self.app = app
        self.owner = owner

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        correlation = uuid.uuid4().hex
        scope.setdefault("state", {})["correlation_id"] = correlation
        started = False

        async def secured_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                headers = list(message.get("headers", []))
                headers.extend([
                    (b"x-content-type-options", b"nosniff"),
                    (b"x-frame-options", b"DENY"),
                    # Keep form Origin headers usable while withholding URL paths and tokens.
                    (b"referrer-policy", b"strict-origin"),
                    (b"cache-control", b"no-store"),
                    (b"content-security-policy", b"default-src 'none'; style-src 'self'; img-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"),
                    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
                    (b"x-request-id", correlation.encode()),
                ])
                if scope.get("scheme") == "https":
                    headers.append((b"strict-transport-security", b"max-age=31536000"))
                message["headers"] = headers
            await send(message)

        async def reject(code, detail):
            await JSONResponse({"detail": detail, "request_id": correlation}, status_code=code)(scope, receive, secured_send)

        try:
            headers = scope.get("headers", [])
            host_values = [v.decode("latin-1").lower() for k, v in headers if k.lower() == b"host"]
            hosts = getattr(self.owner.state, "trusted_hosts", set())
            local_health = scope.get("path") in {"/health/live", "/health/ready"} and len(host_values) == 1 and host_values[0] in {"localhost", "127.0.0.1", "localhost:8000", "127.0.0.1:8000"}
            if len(host_values) != 1 or (host_values[0] not in hosts and not local_health):
                return await reject(400, "Invalid host")
            if scope.get("path") not in {"/health/live", "/health/ready"} and scope.get("scheme") != "https":
                return await reject(400, "HTTPS required")
            if len(scope.get("query_string", b"")) > 4096:
                return await reject(414, "Request too large")
            lengths = [v for k, v in headers if k.lower() == b"content-length"]
            transfers = [v for k, v in headers if k.lower() == b"transfer-encoding"]
            if len(lengths) > 1 or len(transfers) > 1 or (lengths and transfers):
                return await reject(400, "Invalid request length")
            length = None
            if lengths:
                if not lengths[0].isdigit() or len(lengths[0]) > 10:
                    return await reject(400, "Invalid request length")
                length = int(lengths[0])
                if length < 0 or length > MAX_BODY_BYTES:
                    return await reject(413, "Request too large")
            body = bytearray()
            deadline = time.monotonic() + BODY_READ_SECONDS
            try:
                async with asyncio.timeout(BODY_READ_SECONDS):
                    for _ in range(MAX_BODY_CHUNKS):
                        if time.monotonic() >= deadline:
                            return await reject(408, "Request body timed out")
                        message = await receive()
                        if message["type"] == "http.disconnect":
                            return
                        body.extend(message.get("body", b""))
                        if len(body) > MAX_BODY_BYTES:
                            return await reject(413, "Request too large")
                        if not message.get("more_body", False):
                            break
                    else:
                        return await reject(413, "Request is too fragmented")
            except TimeoutError:
                return await reject(408, "Request body timed out")
            if length is not None and length != len(body):
                return await reject(400, "Invalid request length")
            delivered = False

            async def buffered_receive():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": bytes(body), "more_body": False}
                return await receive()

            await self.app(scope, buffered_receive, secured_send)
        except Exception:
            # Never log exception messages, query strings, tokens, email addresses or credentials.
            logger.error("request_failed category=internal request_id=%s", correlation)
            if not started:
                await reject(500, "Request failed")


def _redirect(message: str) -> RedirectResponse:
    # Fixed message codes keep personal data out of redirects, browser history and access logs.
    return RedirectResponse("/?notice=" + message, status_code=303)


def _origin(request: Request) -> None:
    origins = request.headers.getlist("origin")
    if len(origins) != 1 or origins[0] != request.app.state.origin:
        raise HTTPException(403, "Request origin rejected")


async def _admin(request: Request) -> None:
    address = request.client.host if request.client else "unknown"
    limiter = request.app.state.login_limiter
    reservation = limiter.reserve(address)
    if reservation is None:
        raise HTTPException(429, "Too many authentication attempts", headers={"Retry-After": "60"})
    try:
        authorization = request.headers.getlist("authorization")
        if len(authorization) != 1:
            raise ValueError
        kind, encoded = authorization[0].split(" ", 1)
        if kind.lower() != "basic" or len(encoded) > 4096:
            raise ValueError
        username, password = base64.b64decode(encoded, validate=True).decode("utf-8").split(":", 1)
    except (ValueError, TypeError, UnicodeError):
        limiter.finish(reservation, success=False)
        raise HTTPException(401, "Authentication required", headers={"WWW-Authenticate": 'Basic realm="Flight Alerts", charset="UTF-8"'}) from None

    def authenticate():
        authenticated = False
        try:
            username_ok = hmac.compare_digest(username.encode(), request.app.state.runtime.admin_username.encode())
            password_ok = verify_password(password, request.app.state.runtime.admin_password_hash)
            authenticated = username_ok and password_ok
            return authenticated
        finally:
            # Release from the hashing thread, so a disconnected/canceled
            # request cannot free a slot while its scrypt work is still active.
            limiter.finish(reservation, success=authenticated)

    # The dispatched task keeps its reservation even if its client disconnects.
    # Only two bounded hashes can exist; there is no unbounded verification queue.
    if await asyncio.shield(run_in_threadpool(authenticate)):
        return
    raise HTTPException(401, "Authentication required", headers={"WWW-Authenticate": 'Basic realm="Flight Alerts", charset="UTF-8"'})


async def _admin_csrf(request: Request) -> None:
    _origin(request)
    form = await request.form()
    if not request.app.state.csrf.verify(request.cookies.get("admin_csrf", ""), str(form.get("csrf_token", "")), "admin"):
        raise HTTPException(403, "Invalid form token; reload this page")


def _quota(request: Request, action: str, per_minute: int = 5, daily: int = 100) -> None:
    if not request.app.state.db.provider_budget("admin_" + action, per_minute, daily):
        raise HTTPException(429, "Action limit reached", headers={"Retry-After": "60"})


def _config(request: Request) -> dict:
    return request.app.state.db.get_config(request.app.state.default_config)


def _edit_config(request: Request, version: int, mutation) -> None:
    from settings import validate_config
    db = request.app.state.db
    if db.get_config_version() != version:
        raise HTTPException(409, "Settings changed; reload this page before saving")
    cfg = copy.deepcopy(_config(request))
    mutation(cfg)
    try:
        cfg = validate_config(cfg)
    except ValueError:
        raise HTTPException(422, "Invalid settings; review your changes") from None
    try:
        db.save_config(cfg, expected_version=version)
    except ValueError:
        raise HTTPException(409, "Settings changed; reload this page before saving") from None


def _set_csrf(response: Response, name: str, token: str) -> None:
    response.set_cookie(name, token, max_age=3600, secure=True, httponly=True, samesite="strict", path="/")


def _safe_snapshot(snapshot: dict) -> dict:
    from fetcher.base import sanitize_booking_url
    result = copy.deepcopy(snapshot)
    for collection in ("alerts", "recent_deals"):
        for row in result.get(collection, []):
            try:
                row["booking_url"] = sanitize_booking_url(row.get("booking_url", "")) or "https://www.google.com/travel/flights"
            except (ValueError, TypeError):
                row["booking_url"] = "https://www.google.com/travel/flights"
    return result


def create_app(runtime=None, database=None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application):
        from db.crud import DatabaseManager
        from notifier.tokens import TokenSigner
        from settings import RuntimeSettings, load_config
        effective = runtime if runtime is not None else RuntimeSettings.from_env()
        if not 1 <= len(effective.admin_username) <= 128 or any(ord(c) < 32 or ord(c) == 127 or c == ":" for c in effective.admin_username) or not valid_password_hash(effective.admin_password_hash) or len(effective.token_secret) < 32:
            raise RuntimeError("Admin credentials and a strong token secret must be configured")
        try:
            origin = canonical_https_origin(effective.public_base_url)
        except ValueError:
            raise RuntimeError("PUBLIC_BASE_URL must be an HTTPS origin") from None
        public = urlsplit(origin)
        application.state.runtime = effective
        application.state.origin = origin
        application.state.trusted_hosts = {public.netloc}
        application.state.csrf = CSRFSigner(effective.token_secret)
        application.state.tokens = TokenSigner(effective.token_secret)
        application.state.login_limiter = LoginLimiter()
        application.state.default_config = load_config(effective.config_path)
        application.state.db = database if database is not None else DatabaseManager(effective.database_url)
        application.state.db.get_config(application.state.default_config)
        try:
            yield
        finally:
            if database is None:
                application.state.db.close()

    application = FastAPI(title="Flight Alerts", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    application.add_middleware(SecurityMiddleware, owner=application)

    @application.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc):
        return JSONResponse({"detail": "Invalid request", "request_id": request.state.correlation_id}, status_code=422)

    @application.exception_handler(Exception)
    async def internal_error(request: Request, exc):
        logger.error("request_failed category=internal request_id=%s", request.state.correlation_id)
        return JSONResponse({"detail": "Request failed", "request_id": request.state.correlation_id}, status_code=500)

    @application.get("/health/live")
    def live():
        return {"status": "ok"}

    @application.get("/health/ready")
    def ready(request: Request):
        try:
            request.app.state.db.health()
        except Exception:
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return {"status": "ready"}

    @application.get("/assets/app.css")
    def stylesheet():
        return Response(CSS, media_type="text/css")

    admin = APIRouter(dependencies=[Depends(_admin)])

    @admin.get("/", response_class=HTMLResponse)
    @admin.get("/history", response_class=HTMLResponse)
    @admin.get("/providers", response_class=HTMLResponse)
    def index(request: Request, notice: str = ""):
        version = request.app.state.db.get_config_version()
        cfg = _config(request)
        blocked = {str(value).strip().lower() for value in cfg["filters"].get("blocked_destinations", [])}
        active = [d["code"] for d in cfg["monitored_destinations"] if d["code"].lower() not in blocked and d["city"].lower() not in blocked]
        snapshot = _safe_snapshot(request.app.state.db.dashboard_snapshot(active, active_origins=cfg["scanner"]["origins"]))
        token = request.app.state.csrf.issue("admin")
        messages = {"saved": "Settings saved.", "invited": "Verification email queued; confirmation is required before alerts.", "confirmed": "This recipient is already confirmed.", "disabled": "Recipient disabled.", "queued": "Work queued for the worker.", "busy": "A scan is already queued, running, or in its cooldown period.", "watch": "Flight watch saved.", "removed": "Item removed."}
        response = HTMLResponse(TEMPLATES.get_template("dashboard.html").render(
            cfg=cfg, config_version=version, csrf_token=token, snapshot=snapshot,
            recipients=request.app.state.db.list_recipients(), watches=request.app.state.db.list_watched_flights(),
            notice=messages.get(notice, ""),
        ))
        _set_csrf(response, "admin_csrf", token)
        return response

    @admin.post("/scan", dependencies=[Depends(_admin_csrf)])
    def scan(request: Request):
        job_id = request.app.state.db.enqueue_scan(cooldown_seconds=300)
        return _redirect("queued" if job_id is not None else "busy")

    @admin.post("/emails/add", dependencies=[Depends(_admin_csrf)])
    def invite(request: Request, email: str = Form(...)):
        from notifier.emailer import normalize_email
        try:
            email = normalize_email(email)
        except ValueError:
            raise HTTPException(422, "Invalid email address") from None
        _quota(request, "invite")
        if len(request.app.state.db.list_recipients()) >= 100:
            raise HTTPException(409, "Recipient limit reached")
        try:
            recipient = request.app.state.db.invite_recipient(email)
        except ValueError:
            raise HTTPException(409, "Recipient could not be invited; review the recipient list") from None
        if not recipient["enabled"]:
            raise HTTPException(409, "Recipient is disabled; do not override an unsubscribe preference")
        if recipient["verified"]:
            return _redirect("confirmed")
        request.app.state.db.enqueue_notification(recipient["id"], "verification", {}, "verify:" + uuid.uuid4().hex)
        return _redirect("invited")

    @admin.post("/emails/resend", dependencies=[Depends(_admin_csrf)])
    def resend(request: Request, recipient_id: int = Form(...)):
        _quota(request, "invite")
        recipient = request.app.state.db.get_recipient(recipient_id)
        if not recipient or not recipient["enabled"] or recipient["verified"]:
            raise HTTPException(409, "Recipient is not awaiting confirmation")
        request.app.state.db.enqueue_notification(recipient_id, "verification", {}, "verify:" + uuid.uuid4().hex)
        return _redirect("invited")

    @admin.post("/emails/remove", dependencies=[Depends(_admin_csrf)])
    def remove_email(request: Request, recipient_id: int = Form(...)):
        request.app.state.db.disable_recipient(recipient_id)
        return _redirect("disabled")

    @admin.post("/emails/test", dependencies=[Depends(_admin_csrf)])
    def test_emails(request: Request):
        _quota(request, "test", per_minute=1, daily=10)
        snapshot = request.app.state.db.dashboard_snapshot([], freshness_hours=24)
        alerts = snapshot.get("alerts", [])
        if not alerts or not isinstance(alerts[0].get("payload"), dict):
            raise HTTPException(409, "No recent alert is available to queue")
        batch = uuid.uuid4().hex
        for recipient in request.app.state.db.list_recipients():
            if recipient["enabled"] and recipient["verified"]:
                request.app.state.db.enqueue_notification(recipient["id"], "deal", alerts[0]["payload"], f"test:{batch}:{recipient['id']}")
        return _redirect("queued")

    @admin.post("/settings", dependencies=[Depends(_admin_csrf)])
    def settings(request: Request, config_version: int = Form(...), threshold: float = Form(...),
                 freshness_hours: int = Form(...), alerts_paused: str = Form(""), digest_enabled: str = Form(""),
                 digest_interval_hours: int = Form(6), quiet_enabled: str = Form(""), quiet_start: int = Form(22),
                 quiet_end: int = Form(7), quiet_timezone: str = Form("America/Denver"),
                 price_improvement_usd: float | None = Form(None),
                 price_improvement_percent: float | None = Form(None),
                 deduplication_window_hours: int | None = Form(None),
                 scan_interval_hours: int | None = Form(None)):
        if any(value not in {"", "on"} for value in (alerts_paused, digest_enabled, quiet_enabled)):
            raise HTTPException(422, "Invalid checkbox value")
        def mutate(cfg):
            filters = cfg.setdefault("filters", {})
            filters.update(discount_threshold_percent=threshold, freshness_hours=freshness_hours,
                           alerts_paused=alerts_paused == "on", digest_enabled=digest_enabled == "on",
                           digest_interval_hours=digest_interval_hours)
            filters["quiet_hours"] = {"start": quiet_start, "end": quiet_end, "timezone": quiet_timezone} if quiet_enabled == "on" else None
            for name, value in (
                ("price_improvement_usd", price_improvement_usd),
                ("price_improvement_percent", price_improvement_percent),
                ("deduplication_window_hours", deduplication_window_hours),
            ):
                if value is not None:
                    filters[name] = value
            if scan_interval_hours is not None:
                cfg["scanner"]["scan_interval_hours"] = scan_interval_hours
        _edit_config(request, config_version, mutate)
        return _redirect("saved")

    @admin.post("/airports/add", dependencies=[Depends(_admin_csrf)])
    def add_airport(request: Request, config_version: int = Form(...), code: str = Form(...), city: str = Form(...),
                    country_code: str = Form(...), slc_baseline: float = Form(800), las_baseline: float = Form(720)):
        clean_code, clean_country, clean_city = code.strip().upper(), country_code.strip().upper(), city.strip()
        if not re.fullmatch(r"[A-Z]{3}", clean_code) or not re.fullmatch(r"[A-Z]{2}", clean_country) or not 1 <= len(clean_city) <= 100 or any(ord(c) < 32 for c in clean_city):
            raise HTTPException(422, "Invalid airport details")
        def mutate(cfg):
            destinations = cfg.setdefault("monitored_destinations", [])
            item = next((d for d in destinations if d["code"] == clean_code), None)
            if item is None:
                item = {"code": clean_code}
                destinations.append(item)
            item.update(city=clean_city, country_code=clean_country, baseline_avg_usd={"SLC": slc_baseline, "LAS": las_baseline})
            item.pop("baseline_avg_USD", None)
            if clean_country == "US":
                allowed = cfg["filters"].setdefault("allowed_domestic_airports", [])
                if clean_code not in allowed:
                    allowed.append(clean_code)
        _edit_config(request, config_version, mutate)
        return _redirect("saved")

    @admin.post("/airports/remove", dependencies=[Depends(_admin_csrf)])
    def remove_airport(request: Request, config_version: int = Form(...), code: str = Form(...)):
        clean_code = code.strip().upper()
        def mutate(cfg):
            cfg["monitored_destinations"] = [d for d in cfg["monitored_destinations"] if d["code"] != clean_code]
            cfg["filters"]["allowed_domestic_airports"] = [a for a in cfg["filters"].get("allowed_domestic_airports", []) if a != clean_code]
        _edit_config(request, config_version, mutate)
        return _redirect("removed")

    @admin.post("/blocklist/add", dependencies=[Depends(_admin_csrf)])
    def add_blocklist(request: Request, config_version: int = Form(...), term: str = Form(...)):
        cleaned = term.strip()
        if not 1 <= len(cleaned) <= 100 or any(ord(c) < 32 for c in cleaned):
            raise HTTPException(422, "Invalid blocklist entry")
        def mutate(cfg):
            terms = cfg["filters"].setdefault("blocked_destinations", [])
            if cleaned.lower() not in {v.lower() for v in terms}:
                terms.append(cleaned)
        _edit_config(request, config_version, mutate)
        return _redirect("saved")

    @admin.post("/blocklist/remove", dependencies=[Depends(_admin_csrf)])
    def remove_blocklist(request: Request, config_version: int = Form(...), term: str = Form(...)):
        def mutate(cfg):
            cfg["filters"]["blocked_destinations"] = [v for v in cfg["filters"].get("blocked_destinations", []) if v.lower() != term.strip().lower()]
        _edit_config(request, config_version, mutate)
        return _redirect("removed")

    @admin.post("/watches/add", dependencies=[Depends(_admin_csrf)])
    def add_watch(request: Request, flight_iata: str = Form(...), flight_date: str = Form(...)):
        code = flight_iata.strip().upper()
        try:
            departure = date.fromisoformat(flight_date)
        except ValueError:
            raise HTTPException(422, "Invalid flight date") from None
        if not re.fullmatch(r"[A-Z0-9]{2}[0-9]{1,4}[A-Z]?", code) or not datetime.now(timezone.utc).date() <= departure <= datetime.now(timezone.utc).date() + timedelta(days=365):
            raise HTTPException(422, "Invalid flight identifier or date")
        if len(request.app.state.db.list_watched_flights()) >= 25:
            raise HTTPException(409, "Flight watch limit reached")
        try:
            request.app.state.db.watch_flight(code, departure.isoformat())
        except ValueError:
            raise HTTPException(409, "Flight watch could not be saved; review the current list") from None
        return _redirect("watch")

    @admin.post("/watches/remove", dependencies=[Depends(_admin_csrf)])
    def remove_watch(request: Request, watch_id: int = Form(...)):
        request.app.state.db.delete_watch(watch_id)
        return _redirect("removed")

    application.include_router(admin)

    def subscription_id(request: Request, token: str, purpose: str) -> int:
        try:
            recipient_id = request.app.state.tokens.verify(token, purpose)
            if not request.app.state.db.get_recipient(recipient_id):
                raise ValueError
            return recipient_id
        except ValueError:
            raise HTTPException(400, "This link is invalid or expired") from None

    def public_scope(token: str, purpose: str) -> str:
        return "subscription:" + purpose + ":" + hashlib.sha256(token.encode()).hexdigest()

    @application.get("/subscriptions/{purpose}", response_class=HTMLResponse)
    def subscription_form(request: Request, purpose: str, token: str = ""):
        if purpose not in {"confirm", "unsubscribe"}:
            raise HTTPException(404, "Not found")
        subscription_id(request, token, purpose)
        csrf = request.app.state.csrf.issue(public_scope(token, purpose))
        response = HTMLResponse(TEMPLATES.get_template("subscription.html").render(purpose=purpose, token=token, csrf_token=csrf, completed=False))
        _set_csrf(response, "subscription_csrf", csrf)
        return response

    @application.post("/subscriptions/{purpose}", response_class=HTMLResponse)
    def subscription_action(request: Request, purpose: str, token: str = Form(...), csrf_token: str = Form(...)):
        if purpose not in {"confirm", "unsubscribe"}:
            raise HTTPException(404, "Not found")
        _origin(request)
        recipient_id = subscription_id(request, token, purpose)
        if not request.app.state.csrf.verify(request.cookies.get("subscription_csrf", ""), csrf_token, public_scope(token, purpose)):
            raise HTTPException(403, "Invalid form token; reopen the subscription link")
        if purpose == "confirm":
            changed = request.app.state.db.confirm_recipient(recipient_id)
            if not changed:
                raise HTTPException(400, "This subscription cannot be confirmed")
        else:
            request.app.state.db.disable_recipient(recipient_id)
        response = HTMLResponse(TEMPLATES.get_template("subscription.html").render(purpose=purpose, completed=True))
        response.delete_cookie("subscription_csrf", path="/", secure=True, httponly=True, samesite="strict")
        return response

    return application


app = create_app()
