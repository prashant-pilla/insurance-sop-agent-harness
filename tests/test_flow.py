import json

from app.llm.base import LLMRequestError
from app.sop.state import Extraction
from tests.conftest import DEMO_EXTRACTION, DEMO_UTTERANCE, Harness

RAW_VALUES = ("1985-03-15", "4472")


def test_demo_utterance_runs_three_phases_in_one_turn(make_harness, tmp_path):
    harness: Harness = make_harness({"margaret chen, policy pol-9921": DEMO_EXTRACTION})
    result = harness.say(DEMO_UTTERANCE)
    state = harness.state
    assert state.verified is True
    assert state.phase == "PROCESS_CASE"
    assert state.active_case_id == "CL-2048"
    assert state.intent_path == "denial_question"
    assert state.memory.case_hints.model_dump() == {
        "case_type": "healthcare",
        "status": "denied",
        "time_hint": "January",
        "case_id": None,
    }
    assert "CL-2048" in result.reply
    assert "pathology report" in result.reply
    assert result.debug is not None
    decisions = result.debug["decisions"]
    assert any(d.startswith("VERIFY_ID: matched 3 factors") for d in decisions)
    assert any("RESOLVE_INTENT" in d and "CL-2048" in d for d in decisions)
    assert any(d.startswith("PROCESS_CASE: answering") for d in decisions)
    assert result.debug["guard"]["checked"] is False
    facts = result.debug["directive"]["facts"]
    assert facts["claim"]["denial_reason"].startswith("the review file did not include")
    assert {c["case_id"] for c in facts["other_claims"]} == {"CL-2011", "CL-1899", "CL-2102"}
    trace_file = tmp_path / "traces" / f"{state.session_id}.jsonl"
    assert trace_file.exists()
    entry = json.loads(trace_file.read_text(encoding="utf-8").splitlines()[-1])
    assert entry["phase_before"] == "VERIFY_ID" and entry["phase_after"] == "PROCESS_CASE"


def test_stored_user_turn_is_redacted_and_debug_extraction_is_masked(make_harness, tmp_path):
    harness: Harness = make_harness({"margaret chen, policy pol-9921": DEMO_EXTRACTION})
    result = harness.say(DEMO_UTTERANCE)
    state = harness.state

    user_turns = [turn.text for turn in state.transcript if turn.role == "user"]
    assert user_turns == [
        "I'm the policyholder. My name is Margaret Chen, policy POL-9921. I'm calling about my denied "
        "healthcare claim from January. DOB is [DATE], SSN last four is [ID4]."
    ]
    for raw in RAW_VALUES:
        assert not any(raw in text for text in user_turns)
    # The extractor itself still worked on the raw message: identity was verified from it.
    assert state.verified is True and state.slots.dob == "1985-03-15" and state.slots.id_last4 == "4472"

    assert result.debug is not None
    extraction = result.debug["extraction"]
    assert set(extraction) == set(Extraction.model_fields)
    assert extraction["dob"] == "[redacted]" and extraction["id_last4"] == "[redacted]"
    assert extraction["phone"] is None and extraction["email"] is None
    assert extraction["name"] == "Margaret Chen" and extraction["policy_number"] == "POL-9921"

    trace_text = (tmp_path / "traces" / f"{state.session_id}.jsonl").read_text(encoding="utf-8")
    for raw in RAW_VALUES:
        assert raw not in trace_text


def test_responder_and_email_drafter_never_receive_raw_identity_values(make_harness):
    harness: Harness = make_harness({"margaret chen, policy pol-9921": DEMO_EXTRACTION})
    harness.say(DEMO_UTTERANCE)
    harness.llm.scripts["that's all"] = {"conversation_done": True}
    harness.say("Ok, that's all I needed")
    harness.llm.scripts["yes please"] = {"email_consent": "yes"}
    harness.say("Yes please")
    assert harness.state.phase == "CLOSED" and len(harness.state.outbox) == 1

    checked = [p for p in harness.llm.prompts if p["role"] in ("responder", "email_summary")]
    assert {p["role"] for p in checked} == {"responder", "email_summary"}
    for prompt in checked:
        blob = prompt["system"] + json.dumps(prompt["messages"])
        for raw in RAW_VALUES:
            assert raw not in blob, f"{prompt['role']} prompt leaked {raw}"
    # The most recent call is the responder's closing reply; its history carries the redacted turn.
    assert harness.llm.last_system and harness.llm.last_messages
    history_blob = json.dumps(harness.llm.last_messages)
    assert "[DATE]" in history_blob and "[ID4]" in history_blob
    # The extractor did see the raw message on the first turn.
    first_extractor = next(p for p in harness.llm.prompts if p["role"] == "extractor")
    assert "1985-03-15" in first_extractor["messages"][0]["content"]


def test_pre_extraction_exits_still_redact_the_stored_turn(make_harness, monkeypatch):
    harness: Harness = make_harness({})
    llm = harness.llm

    def broken(system: str, messages: list, *, max_tokens: int = 700):
        raise LLMRequestError(500, "down")

    monkeypatch.setattr(llm, "complete_json", broken)
    harness.say("Margaret Chen, DOB 03/15/1985, phone 650-521-2836, margaret@email.com")
    assert harness.state.transcript[-2].role == "user"
    assert harness.state.transcript[-2].text == "Margaret Chen, DOB [DATE], phone [PHONE], [EMAIL]"


def test_memory_carries_case_hints_across_phases(make_harness):
    harness: Harness = make_harness(
        {
            "calling about": {
                "name": "Margaret Chen",
                "policy_number": "POL-9921",
                "intent_hints": ["denied healthcare claim"],
                "intent_path": "denial_question",
                "case_hints": {"case_type": "healthcare", "status": "denied", "time_hint": "January"},
            },
            "last four": {"dob": "03/15/1985", "id_last4": "4472"},
        }
    )
    first = harness.say("Hi, I'm Margaret Chen, policy POL-9921, calling about my denied healthcare claim from January")
    assert harness.state.phase == "VERIFY_ID"
    assert harness.state.memory.case_hints.status == "denied"
    assert "CL-2048" not in first.reply

    harness.say("Sure, DOB 03/15/1985 and last four 4472")
    assert harness.state.phase == "PROCESS_CASE"
    assert harness.state.active_case_id == "CL-2048"
    assert harness.state.intent_path == "denial_question"


def test_stated_reason_is_surfaced_to_responder_in_every_phase(make_harness):
    """A reason given during verification must reach the responder later, not just the state."""
    harness: Harness = make_harness(
        {
            "this is margaret": {"name": "Margaret Chen"},
            "entered in wrong": {"intent_hints": ["insurance number entered wrong"], "intent_path": "general_claim_question"},
            "last four": {"dob": "1985-03-15", "id_last4": "4472"},
            "dental": {"case_hints": {"case_type": "dental"}},
        }
    )
    harness.say("Hi, this is Margaret Chen")
    during_verify = harness.say("I think my insurance number was entered in wrong")
    assert harness.state.phase == "VERIFY_ID"
    verify_facts = during_verify.debug["directive"]["facts"]
    assert verify_facts["caller_context"]["stated_reasons_for_calling"] == ["insurance number entered wrong"]
    assert "CL-" not in during_verify.reply

    resolve = harness.say("DOB is 1985-03-15, SSN last four is 4472")
    assert harness.state.phase == "RESOLVE_INTENT"
    assert "insurance number entered wrong" in json.dumps(resolve.debug["directive"]["facts"]["caller_context"])

    process = harness.say("dental")
    assert harness.state.phase == "PROCESS_CASE"
    assert harness.state.active_case_id == "CL-1899"
    facts = process.debug["directive"]["facts"]
    assert "insurance number entered wrong" in facts["caller_context"]["stated_reasons_for_calling"]
    assert facts["policy_number_on_file"] == "POL-9921"
    assert any("stated_reasons_for_calling" in i for i in process.debug["directive"]["instructions"])


def test_disambiguation_when_hints_match_multiple_claims(make_harness):
    harness: Harness = make_harness(
        {
            "margaret": {
                "name": "Margaret Chen",
                "policy_number": "POL-9921",
                "dob": "1985-03-15",
                "id_last4": "4472",
                "case_hints": {"case_type": "healthcare", "time_hint": "January"},
            },
            "denied one": {"case_hints": {"status": "denied"}, "intent_path": "status_inquiry"},
        }
    )
    result = harness.say("Margaret Chen, POL-9921, 1985-03-15, 4472, about my healthcare claim from January")
    assert harness.state.phase == "RESOLVE_INTENT"
    assert harness.state.active_case_id is None
    assert result.debug is not None
    listed = {c["case_id"] for c in result.debug["directive"]["facts"]["claims"]}
    assert listed == {"CL-2048", "CL-2011"}

    harness.say("The denied one")
    assert harness.state.phase == "PROCESS_CASE"
    assert harness.state.active_case_id == "CL-2048"
    assert harness.state.intent_path == "status_inquiry"


def test_no_hints_asks_what_they_need(make_harness):
    harness: Harness = make_harness(
        {"margaret": {"name": "Margaret Chen", "policy_number": "POL-9921", "dob": "1985-03-15", "id_last4": "4472"}}
    )
    result = harness.say("Margaret Chen, POL-9921, 1985-03-15, 4472")
    assert harness.state.phase == "RESOLVE_INTENT"
    assert result.debug is not None
    assert len(result.debug["directive"]["facts"]["claims"]) == 4


def test_switch_claim_within_process_case(verified_harness: Harness):
    verified_harness.llm.scripts["auto claim"] = {"case_hints": {"case_type": "auto"}, "intent_path": "status_inquiry"}
    result = verified_harness.say("Actually, what about my auto claim?")
    assert verified_harness.state.phase == "PROCESS_CASE"
    assert verified_harness.state.active_case_id == "CL-2102"
    assert "CL-2102" in result.reply


def test_closed_then_new_question_returns_to_process_case(verified_harness: Harness):
    verified_harness.llm.scripts["that's all"] = {"conversation_done": True}
    verified_harness.llm.scripts["no thanks"] = {"email_consent": "no"}
    verified_harness.llm.scripts["documents"] = {"intent_path": "document_submission", "scope": "in_scope_claim"}
    verified_harness.say("Ok that's all for now")
    assert verified_harness.state.phase == "POST_PROCESS"
    verified_harness.say("No thanks")
    assert verified_harness.state.phase == "CLOSED"
    result = verified_harness.say("Wait, which documents do I need to send?")
    assert verified_harness.state.phase == "PROCESS_CASE"
    assert verified_harness.state.active_case_id == "CL-2048"
    assert "pathology report" in result.reply
