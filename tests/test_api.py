from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.llm.base import LLMRequestError
from app.sop.prompts import MODEL_NOT_CONFIGURED_REPLY, MODEL_UNAVAILABLE_REPLY
from tests.conftest import DEMO_EXTRACTION, DEMO_UTTERANCE, FakeLLM, make_settings

STATE_KEYS = {
    "phase",
    "verified",
    "verified_factors",
    "policyholder_name",
    "memory",
    "active_case_id",
    "intent_path",
    "verify_attempts",
    "off_topic_streak",
    "frustration_streak",
    "last_emotion",
    "outbox",
    "handoff_note",
}


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    fake = FakeLLM({"margaret chen, policy pol-9921": DEMO_EXTRACTION})
    monkeypatch.setattr(main_module, "build_llm", lambda settings: fake)
    return TestClient(main_module.create_app(make_settings(tmp_path)))


def test_healthz(client: TestClient):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "provider": "openai", "model": "fake-model", "llm_configured": True}


def test_healthz_reports_missing_key(tmp_path: Path):
    client = TestClient(main_module.create_app(make_settings(tmp_path, llm_api_key=None)))
    payload = client.get("/healthz").json()
    assert payload["status"] == "ok"
    assert payload["llm_configured"] is False


def test_session_lifecycle(client: TestClient):
    created = client.post("/api/session", json={})
    assert created.status_code == 200
    body = created.json()
    assert set(body) == {"session_id", "reply", "state", "debug"}
    assert body["debug"] is None
    assert "verify" in body["reply"].lower()
    assert set(body["state"]) == STATE_KEYS
    assert body["state"]["phase"] == "VERIFY_ID"
    session_id = body["session_id"]

    replied = client.post(f"/api/session/{session_id}/message", json={"message": DEMO_UTTERANCE})
    assert replied.status_code == 200
    payload = replied.json()
    assert set(payload) == {"reply", "state", "debug"}
    assert payload["state"]["phase"] == "PROCESS_CASE"
    assert payload["state"]["active_case_id"] == "CL-2048"
    assert payload["state"]["policyholder_name"] == "Margaret Chen"
    assert set(payload["debug"]) == {"extraction", "decisions", "directive", "guard", "llm_calls"}
    assert {c["role"] for c in payload["debug"]["llm_calls"]} == {"extractor", "responder"}

    fetched = client.get(f"/api/session/{session_id}")
    assert fetched.status_code == 200
    fetched_body = fetched.json()
    transcript = fetched_body["transcript"]
    assert [t["role"] for t in transcript] == ["agent", "user", "agent"]
    # The stored user turn is the redacted copy: identity values never come back out of the API.
    assert transcript[1]["text"] == DEMO_UTTERANCE.replace("1985-03-15", "[DATE]").replace("4472", "[ID4]")
    assert "1985-03-15" not in json.dumps(fetched_body) and "4472" not in json.dumps(fetched_body)
    assert fetched_body["last_debug"] == payload["debug"]


def test_unknown_session_is_404(client: TestClient):
    assert client.get("/api/session/nope").status_code == 404
    assert client.post("/api/session/nope/message", json={"message": "hi"}).status_code == 404


def test_debug_is_null_when_tracing_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(main_module, "build_llm", lambda settings: FakeLLM({}))
    client = TestClient(main_module.create_app(make_settings(tmp_path, trace_enabled=False)))
    session_id = client.post("/api/session", json={}).json()["session_id"]
    payload = client.post(f"/api/session/{session_id}/message", json={"message": "hello"}).json()
    assert payload["debug"] is None
    assert client.get(f"/api/session/{session_id}").json()["last_debug"] is None
    assert not (tmp_path / "traces").exists()


def test_missing_api_key_returns_friendly_reply(tmp_path: Path):
    client = TestClient(main_module.create_app(make_settings(tmp_path, llm_api_key=None)))
    assert client.get("/healthz").status_code == 200
    session_id = client.post("/api/session", json={}).json()["session_id"]
    payload = client.post(f"/api/session/{session_id}/message", json={"message": "hello"}).json()
    assert payload["reply"] == MODEL_NOT_CONFIGURED_REPLY
    assert payload["state"]["phase"] == "VERIFY_ID"


def test_missing_static_dir_does_not_crash(tmp_path: Path):
    client = TestClient(main_module.create_app(make_settings(tmp_path, static_dir=tmp_path / "missing")))
    assert client.get("/healthz").status_code == 200
    assert client.get("/").status_code == 404


def test_root_serves_the_chat_ui(client: TestClient):
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "SOP State" in response.text
    assert client.get("/static/app.js").status_code == 200


def test_config_error_from_provider_returns_notice_and_leaves_state_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A 401/403/404 from the provider (bad key, wrong model) is surfaced as a fixed notice, not a phase reply."""

    class RejectingLLM(FakeLLM):
        def complete_json(self, system: str, messages: list, *, max_tokens: int = 700) -> dict:
            raise LLMRequestError(401, "invalid api key")

    monkeypatch.setattr(main_module, "build_llm", lambda settings: RejectingLLM())
    client = TestClient(main_module.create_app(make_settings(tmp_path)))
    session_id = client.post("/api/session", json={}).json()["session_id"]
    payload = client.post(f"/api/session/{session_id}/message", json={"message": DEMO_UTTERANCE}).json()
    assert payload["reply"] == MODEL_UNAVAILABLE_REPLY.format(status=401)
    assert "HTTP 401" in payload["reply"] and "LLM_API_KEY" in payload["reply"]
    assert payload["state"]["phase"] == "VERIFY_ID"
    assert payload["state"]["verified"] is False
    assert "extractor failed: HTTP 401 (configuration error)" in payload["debug"]["decisions"]
    # The stored user turn is still redacted even though extraction never ran.
    transcript = client.get(f"/api/session/{session_id}").json()["transcript"]
    assert "1985-03-15" not in transcript[1]["text"] and "[DATE]" in transcript[1]["text"]


# --------------------------------------------------------------------------------------------------
# Representative consent flow (appended; existing tests above are unchanged)
# --------------------------------------------------------------------------------------------------

import json  # noqa: E402

from app.sop.state import Extraction  # noqa: E402

REP_UTTERANCE = (
    "Hi, this is David Chen. I'm calling on behalf of my mother, Margaret Chen, about her denied "
    "healthcare claim from January."
)
REP_EXTRACTION = {
    "caller_role": "representative",
    "representative_name": "David Chen",
    "relationship": "son",
    "on_behalf_of_name": "Margaret Chen",
    "intent_hints": ["denied healthcare claim from January"],
    "intent_path": "denial_question",
    "case_hints": {"case_type": "healthcare", "status": "denied", "time_hint": "January"},
}
REP_STATE_KEYS = STATE_KEYS | {"caller_role", "representative_name", "representative_relationship", "consent_status"}


@pytest.fixture
def rep_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    fake = FakeLLM({"on behalf of my mother": REP_EXTRACTION, "margaret chen, policy pol-9921": DEMO_EXTRACTION})
    monkeypatch.setattr(main_module, "build_llm", lambda settings: fake)
    return TestClient(main_module.create_app(make_settings(tmp_path)))


@pytest.mark.parametrize("body", [None, {}, {"consent_scenario": "default"}, {"consent_scenario": "timeout"}])
def test_create_session_accepts_optional_consent_scenario(client: TestClient, body):
    created = client.post("/api/session", json=body) if body is not None else client.post("/api/session")
    assert created.status_code == 200
    payload = created.json()
    assert set(payload) == {"session_id", "reply", "state", "debug"}
    # A fresh session is a normal policyholder session: the representative keys are absent, not null.
    assert set(payload["state"]) == STATE_KEYS
    assert payload["state"]["phase"] == "VERIFY_ID"


def test_create_session_rejects_unknown_consent_scenario(client: TestClient):
    response = client.post("/api/session", json={"consent_scenario": "bogus"})
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail == "unknown consent_scenario 'bogus'; valid values: default, timeout"


def test_representative_session_over_api_default_scenario(rep_client: TestClient, tmp_path: Path):
    session_id = rep_client.post("/api/session", json={"consent_scenario": "default"}).json()["session_id"]

    first = rep_client.post(f"/api/session/{session_id}/message", json={"message": REP_UTTERANCE}).json()
    state = first["state"]
    assert set(state) == REP_STATE_KEYS
    assert state["phase"] == "VERIFY_ID"
    assert state["verified"] is False
    assert state["policyholder_name"] is None
    assert state["caller_role"] == "representative"
    assert state["representative_name"] == "David Chen"
    assert state["representative_relationship"] == "son"
    assert state["consent_status"] == "pending"
    assert "CL-" not in first["reply"]
    assert any(d.startswith("VERIFY_ID/REP:") for d in first["debug"]["decisions"])
    assert "acceptable_factors" not in first["debug"]["directive"]["facts"]

    second = rep_client.post(f"/api/session/{session_id}/message", json={"message": "Okay, has she approved yet?"}).json()
    state = second["state"]
    assert state["consent_status"] == "approved"
    assert state["verified"] is True
    assert state["policyholder_name"] == "Margaret Chen"
    assert state["phase"] == "PROCESS_CASE"
    assert state["active_case_id"] == "CL-2048"
    assert state["verified_factors"] == {f: False for f in ("name", "dob", "phone", "email", "id_last4")}
    assert second["debug"]["directive"]["facts"]["caller"]["role"] == "representative"

    fetched = rep_client.get(f"/api/session/{session_id}").json()
    assert fetched["state"]["caller_role"] == "representative"
    assert fetched["last_debug"] == second["debug"]

    trace_file = tmp_path / "traces" / f"{session_id}.jsonl"
    entries = [json.loads(line) for line in trace_file.read_text(encoding="utf-8").splitlines()]
    assert any(d.startswith("VERIFY_ID/REP:") for entry in entries for d in entry["debug"]["decisions"])
    for entry in entries:
        assert set(entry["debug"]["extraction"]) <= set(Extraction.model_fields)


def test_representative_session_over_api_timeout_scenario(rep_client: TestClient):
    session_id = rep_client.post("/api/session", json={"consent_scenario": "timeout"}).json()["session_id"]
    state = rep_client.post(f"/api/session/{session_id}/message", json={"message": REP_UTTERANCE}).json()["state"]
    assert state["consent_status"] == "pending" and state["caller_role"] == "representative"
    for _ in range(4):
        payload = rep_client.post(f"/api/session/{session_id}/message", json={"message": "Still waiting."}).json()
        assert payload["state"]["consent_status"] == "pending"
        assert payload["state"]["phase"] == "VERIFY_ID"
        assert "CL-" not in payload["reply"]
    final = rep_client.post(f"/api/session/{session_id}/message", json={"message": "Anything yet?"}).json()
    assert final["state"]["phase"] == "HUMAN_HANDOFF"
    assert final["state"]["consent_status"] == "timed_out"
    assert "policyholder consent not received" in final["state"]["handoff_note"]


def test_demo_caller_over_api_has_no_representative_keys(rep_client: TestClient):
    session_id = rep_client.post("/api/session", json={}).json()["session_id"]
    payload = rep_client.post(f"/api/session/{session_id}/message", json={"message": DEMO_UTTERANCE}).json()
    assert payload["state"]["phase"] == "PROCESS_CASE"
    assert set(payload["state"]) == STATE_KEYS
    assert "caller" not in payload["debug"]["directive"]["facts"]
