"""FastAPI entrypoint: session endpoints, health check, static UI."""

from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.config import Settings, load_settings
from app.llm.factory import build_llm
from app.sop.controller import Controller
from app.sop.state import SessionState
from app.sop.trace import Tracer
from app.tools.data import FixtureStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


class MessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)


class SessionCreateRequest(BaseModel):
    """Optional session options; an empty or missing body means the defaults."""

    consent_scenario: str = Field(default="default", min_length=1, max_length=64)


class SessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, SessionState] = {}
        self._lock = threading.Lock()

    def add(self, state: SessionState) -> None:
        with self._lock:
            self._sessions[state.session_id] = state

    def get(self, session_id: str) -> SessionState:
        with self._lock:
            state = self._sessions.get(session_id)
        if state is None:
            raise HTTPException(status_code=404, detail="unknown session")
        return state


def build_controller(settings: Settings) -> Controller:
    store = FixtureStore(settings.fixtures_dir)
    llm = build_llm(settings)
    if llm is None:
        logger.warning("LLM_API_KEY not set: the agent will answer with a not-configured notice")
    tracer = Tracer(settings.trace_enabled, settings.traces_dir)
    return Controller(settings, store, llm, tracer)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    controller = build_controller(settings)
    sessions = SessionStore()
    turn_lock = threading.Lock()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        logger.info("provider=%s model=%s trace=%s", settings.llm_provider, settings.llm_model, settings.trace_enabled)
        yield

    app = FastAPI(title="Northwind Insurance Claims Agent", lifespan=lifespan)

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {
            "status": "ok",
            "provider": settings.llm_provider,
            "model": settings.llm_model,
            "llm_configured": bool(settings.llm_api_key),
        }

    @app.post("/api/session")
    def create_session(body: SessionCreateRequest | None = None) -> dict[str, Any]:
        options = body or SessionCreateRequest()
        try:
            state, greeting = controller.start_session(consent_scenario=options.consent_scenario)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        sessions.add(state)
        return {"session_id": state.session_id, "reply": greeting, "state": state.public_state(), "debug": None}

    @app.post("/api/session/{session_id}/message")
    def post_message(session_id: str, body: MessageRequest) -> dict[str, Any]:
        state = sessions.get(session_id)
        with turn_lock:
            result = controller.handle_turn(state, body.message.strip())
        return {"reply": result.reply, "state": result.state.public_state(), "debug": result.debug}

    @app.get("/api/session/{session_id}")
    def get_session(session_id: str) -> dict[str, Any]:
        state = sessions.get(session_id)
        return {
            "session_id": state.session_id,
            "state": state.public_state(),
            "transcript": [turn.model_dump() for turn in state.transcript],
            "last_debug": state.last_debug,
        }

    index_path = settings.static_dir / "index.html"

    @app.get("/", include_in_schema=False)
    def index() -> Any:
        if index_path.exists():
            return FileResponse(index_path)
        return JSONResponse({"detail": "UI not built yet; see /docs for the API"}, status_code=404)

    if settings.static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(settings.static_dir)), name="static")
    else:
        logger.warning("static directory %s missing; UI disabled", settings.static_dir)

    return app


app = create_app()
