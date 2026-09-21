"""Shared FakeLLM and controller helpers. No network, no API key."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import pytest

from app.config import REPO_ROOT, Settings
from app.llm.base import LLMClient, Message
from app.sop.controller import Controller, TurnResult
from app.sop.prompts import EXTRACTOR_NEW_MESSAGE_MARKER
from app.sop.trace import Tracer
from app.tools.data import FixtureStore

FIXTURES_DIR = REPO_ROOT / "apps" / "insurance_claims" / "fixtures"

DEMO_UTTERANCE = (
    "I'm the policyholder. My name is Margaret Chen, policy POL-9921. I'm calling about my denied "
    "healthcare claim from January. DOB is 1985-03-15, SSN last four is 4472."
)
DEMO_EXTRACTION: dict[str, Any] = {
    "name": "Margaret Chen",
    "policy_number": "POL-9921",
    "dob": "1985-03-15",
    "id_last4": "4472",
    "intent_hints": ["denied healthcare claim from January"],
    "intent_path": "denial_question",
    "case_hints": {"case_type": "healthcare", "status": "denied", "time_hint": "January"},
    "scope": "in_scope_claim",
    "emotion": "neutral",
}

ResponderFn = Callable[[dict[str, Any], bool], str]


def echo_responder(directive: dict[str, Any], strict: bool) -> str:
    """Canned reply that echoes the directive so tests can assert on allowed facts."""
    facts = json.dumps(directive.get("facts", {}), default=str)
    ask = directive.get("must_ask") or directive.get("goal")
    return f"[{directive['phase']}] {ask} FACTS: {facts}"


def parse_directive(system: str) -> dict[str, Any]:
    start = system.index("DIRECTIVE:") + len("DIRECTIVE:")
    payload, _ = json.JSONDecoder().raw_decode(system[start:].lstrip())
    return payload


class FakeLLM(LLMClient):
    provider = "fake"
    model = "fake-model"

    def __init__(self, scripts: dict[str, dict[str, Any]] | None = None, responder: ResponderFn = echo_responder):
        self.scripts = scripts or {}
        self.responder = responder
        self.json_calls: list[str] = []
        self.text_calls: list[bool] = []
        # Full prompt of the most recent call, plus every call by role, so tests can assert on what
        # the model actually received (e.g. that the responder never sees raw identity values).
        self.last_system: str = ""
        self.last_messages: list[Message] = []
        self.prompts: list[dict[str, Any]] = []

    def _record(self, role: str, system: str, messages: list[Message]) -> None:
        self.last_system = system
        self.last_messages = list(messages)
        self.prompts.append({"role": role, "system": system, "messages": list(messages)})

    def complete_json(self, system: str, messages: list[Message], *, max_tokens: int = 700) -> dict[str, Any]:
        if '"subject"' in system:
            self.json_calls.append("email_summary")
            self._record("email_summary", system, messages)
            return {
                "subject": "Summary of your Northwind Insurance claim conversation",
                "body": "We discussed your claim, its status and the next steps.",
            }
        self.json_calls.append("extractor")
        self._record("extractor", system, messages)
        prompt = messages[0]["content"]
        new_message = prompt.split(EXTRACTOR_NEW_MESSAGE_MARKER, 1)[1].strip().lower()
        for key, data in self.scripts.items():
            if key.lower() in new_message:
                return json.loads(json.dumps(data))
        return {}

    def complete_text(
        self, system: str, messages: list[Message], *, max_tokens: int = 400, temperature: float = 0.3
    ) -> str:
        strict = "CRITICAL CORRECTION" in system
        self.text_calls.append(strict)
        self._record("responder", system, messages)
        return self.responder(parse_directive(system), strict)


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "llm_provider": "openai",
        "llm_api_key": "test-key",
        "llm_model": "fake-model",
        "llm_base_url": "http://fake",
        "traces_dir": tmp_path / "traces",
        "fixtures_dir": FIXTURES_DIR,
    }
    values.update(overrides)
    return Settings(**values)


class Harness:
    def __init__(self, controller: Controller, llm: FakeLLM):
        self.controller = controller
        self.llm = llm
        self.state, self.greeting = controller.start_session()

    def say(self, message: str) -> TurnResult:
        return self.controller.handle_turn(self.state, message)


@pytest.fixture
def store() -> FixtureStore:
    return FixtureStore(FIXTURES_DIR)


@pytest.fixture
def make_harness(tmp_path: Path) -> Callable[..., Harness]:
    def _make(
        scripts: dict[str, dict[str, Any]] | None = None,
        responder: ResponderFn = echo_responder,
        **settings_overrides: Any,
    ) -> Harness:
        settings = make_settings(tmp_path, **settings_overrides)
        llm = FakeLLM(scripts, responder)
        controller = Controller(settings, FixtureStore(FIXTURES_DIR), llm, Tracer(settings.trace_enabled, settings.traces_dir))
        return Harness(controller, llm)

    return _make


@pytest.fixture
def verified_harness(make_harness: Callable[..., Harness]) -> Harness:
    """A session that has already run the demo utterance and sits in PROCESS_CASE on CL-2048."""
    harness = make_harness({"margaret chen, policy pol-9921": DEMO_EXTRACTION})
    harness.say(DEMO_UTTERANCE)
    assert harness.state.phase == "PROCESS_CASE"
    return harness
