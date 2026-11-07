"""The public FastAPI app. Requests choose a fixed target and nothing else (docs/PLAN.md V3.5)."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict
from starlette.exceptions import HTTPException

from faultforge.web.limits import client_key
from faultforge.web.replays import SAFE_ID
from faultforge.web.runs import Refused, RunManager
from faultforge.web.viewmodel import View

logger = logging.getLogger("faultforge.web")

STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_REQUEST_BODY_BYTES = 1024
CONTENT_SECURITY_POLICY = "; ".join([
    "default-src 'none'",
    "script-src 'self'",
    "style-src 'self'",
    "img-src 'self'",
    "font-src 'self'",
    "connect-src 'self'",
    "base-uri 'none'",
    "form-action 'none'",
    "frame-ancestors 'none'",
])
SECURITY_HEADERS = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Resource-Policy": "same-origin",
}
HTTP_REASONS = {404: "not_found", 405: "method_not_allowed"}
HTTP_MESSAGES = {404: "There's nothing here.", 405: "That method isn't allowed here."}


class RunRequest(BaseModel):
    """The only input the public app accepts. An extra field is refused, not ignored."""

    model_config = ConfigDict(extra="forbid", strict=True)
    target: Literal["ticket-api", "hostile-build"]


def error_response(status: int, reason: str, message: str, retry_after: float | None = None) -> JSONResponse:
    headers = {"Retry-After": str(max(1, int(retry_after) + 1))} if retry_after is not None else None
    return JSONResponse({"reason": reason, "message": message}, status_code=status, headers=headers)


def _view(view: View, status: int = 200) -> JSONResponse:
    return JSONResponse(view.model_dump(mode="json"), status_code=status)


def _client(request: Request) -> str:
    peer = request.client.host if request.client else None
    return client_key(peer, request.headers.getlist("x-forwarded-for"))


def _is_cross_origin(request: Request) -> bool:
    origin = request.headers.get("origin")
    return bool(origin) and urlsplit(origin).netloc != request.headers.get("host")


def _body_is_too_large(request: Request) -> bool:
    length = request.headers.get("content-length")
    if length is None:
        return False
    return not length.isdecimal() or int(length) > MAX_REQUEST_BODY_BYTES


def _has_body(request: Request) -> bool:
    return request.headers.get("content-length", "0") != "0" or "transfer-encoding" in request.headers


def create_app(manager: RunManager) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await asyncio.to_thread(manager.start)
        try:
            yield
        finally:
            await asyncio.to_thread(manager.stop)

    # No generated docs: they'd need a looser CSP, and nobody outside the page uses this API.
    app = FastAPI(title="FaultForge", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.manager = manager

    @app.middleware("http")
    async def harden(request: Request, call_next) -> Response:
        """Outermost, so that every response has the headers and no error reaches a client as a trace."""
        if request.method == "POST" and _is_cross_origin(request):
            response: Response = error_response(403, "cross_origin", "Requests must come from this site.")
        elif _body_is_too_large(request):
            response = error_response(413, "too_large", "The request body is too large.")
        else:
            try:
                response = await call_next(request)
            except Exception as error:
                logger.error("a request failed unexpectedly (%s)", type(error).__name__)
                response = error_response(500, "internal_error", "Something went wrong on the server.")
        response.headers.update(SECURITY_HEADERS)
        if request.url.path.startswith("/api/") or request.url.path == "/healthz":
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(Refused)
    async def on_refused(_: Request, error: Refused) -> JSONResponse:
        return error_response(error.status, error.reason, error.message, error.retry_after)

    @app.exception_handler(RequestValidationError)
    async def on_invalid(_: Request, __: RequestValidationError) -> JSONResponse:
        # FastAPI's default response echoes the input back, so it's replaced.
        return error_response(422, "invalid_request", 'Send exactly {"target": "ticket-api"} or {"target": "hostile-build"}.')

    @app.exception_handler(HTTPException)
    async def on_http_error(_: Request, error: HTTPException) -> JSONResponse:
        return error_response(
            error.status_code, HTTP_REASONS.get(error.status_code, "http_error"),
            HTTP_MESSAGES.get(error.status_code, "The request couldn't be handled."),
        )

    @app.api_route("/", methods=["GET", "HEAD"])
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", media_type="text/html; charset=utf-8")

    @app.api_route("/healthz", methods=["GET", "HEAD"])
    def healthz() -> JSONResponse:
        if not manager.worker_alive():
            return JSONResponse({"status": "worker_stopped"}, status_code=503)
        return JSONResponse({"status": "ok"})

    @app.get("/api/status")
    def status() -> JSONResponse:
        return _view(manager.status())

    @app.post("/api/runs", status_code=202)
    def create_run(body: RunRequest, request: Request) -> JSONResponse:
        record = manager.submit_run(body.target, _client(request))
        return JSONResponse({"id": record.id, "stage": "queued", "target": body.target}, status_code=202)

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str) -> JSONResponse:
        state = manager.run_state(run_id) if _valid_id(run_id) else None
        if state is None:
            return error_response(404, "not_found", "There's no such run. Only recent runs are kept.")
        return _view(state)

    @app.post("/api/runs/{run_id}/fix", status_code=202)
    def create_fix(run_id: str, request: Request) -> JSONResponse:
        if _has_body(request):
            return error_response(422, "invalid_request", "This endpoint takes no input.")
        record = manager.submit_fix(run_id if _valid_id(run_id) else "", _client(request))
        return JSONResponse({"id": record.id, "stage": "fix_queued"}, status_code=202)

    @app.get("/api/replays")
    def list_replays() -> JSONResponse:
        items = [item.model_dump(mode="json") for item in manager.replays.summaries()]
        return JSONResponse({"replay": True, "replays": items})

    @app.get("/api/replays/{replay_id}")
    def get_replay(replay_id: str) -> JSONResponse:
        replay = manager.replays.get(replay_id) if _valid_id(replay_id) else None
        if replay is None:
            return error_response(404, "not_found", "There's no such replay.")
        return _view(replay)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


def _valid_id(value: str) -> bool:
    return SAFE_ID.fullmatch(value) is not None
