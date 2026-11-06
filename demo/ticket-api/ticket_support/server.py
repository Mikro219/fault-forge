"""Builds the FastAPI app, including the FaultForge test hooks when FF_TEST_HOOKS=1."""

import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from pydantic import BaseModel, Field

from ticket_support import db, fingerprint


class ReserveRequest(BaseModel):
    item_id: int
    buyer: str = Field(min_length=1, max_length=200)


class ResetRequest(BaseModel):
    stock: int = Field(ge=0)


def request_key(request: Request) -> str:
    return request.headers.get("x-ff-request-id") or uuid.uuid4().hex


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        db.init(initial_stock=int(os.environ.get("FF_INITIAL_STOCK", "1")))
        app.state.build = fingerprint.capture()
        yield

    app = FastAPI(title="ticket-api", lifespan=lifespan)
    app.state.in_flight = 0

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    if os.environ.get("FF_TEST_HOOKS") == "1":
        _add_test_hooks(app)
    return app


def _add_test_hooks(app: FastAPI) -> None:
    @app.middleware("http")
    async def count_in_flight(request: Request, call_next):
        if request.url.path.startswith("/__ff") or request.url.path == "/health":
            return await call_next(request)
        app.state.in_flight += 1
        try:
            return await call_next(request)
        finally:
            app.state.in_flight -= 1

    def snapshot() -> dict[str, object]:
        build = app.state.build
        stale = build.stale_source()
        return {
            **db.state(),
            "in_flight": app.state.in_flight,
            "stale_source": stale,
            "build": {
                "build_id": build.build_id,
                "instance_id": build.instance_id,
                "instance_started_at": build.started_at_iso,
                "stale_source": stale,
            },
        }

    @app.post("/__ff/reset")
    async def reset(body: ResetRequest) -> dict[str, object]:
        db.reset(body.stock)
        return snapshot()

    @app.get("/__ff/state")
    async def state() -> dict[str, object]:
        return snapshot()
