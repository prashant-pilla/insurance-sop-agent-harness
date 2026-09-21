"""draft_email_summary: model draft, template fallback, and the caller-PII body check."""

from typing import Any

from app.llm.base import LLMError
from app.sop.prompts import EMAIL_SUMMARY_SYSTEM
from app.sop.responder import (
    EMAIL_NOTE_MODEL_FAILED,
    EMAIL_NOTE_PII_IN_DRAFT,
    contains_caller_pii,
    draft_email_summary,
    fallback_email,
)
from app.sop.state import IdentitySlots, SessionState
from tests.conftest import FakeLLM, Harness

CLAIM: dict[str, Any] = {
    "case_id": "CL-2048",
    "case_type": "healthcare",
    "status": "denied",
    "documents_needed": ["pathology report"],
    "appeal_deadline": "2026-03-15",
}

SLOTS = IdentitySlots(name="Margaret Chen", dob="1985-03-15", phone="6505212836", id_last4="4472", email="margaret@email.com")


def _state() -> SessionState:
    return SessionState(session_id="test", verified=True, policyholder_name="Margaret Chen", slots=SLOTS.model_copy())


class DraftLLM(FakeLLM):
    def __init__(self, body: str | Exception):
        super().__init__()
        self.body = body

    def complete_json(self, system: str, messages: list, *, max_tokens: int = 700) -> dict[str, Any]:
        if isinstance(self.body, Exception):
            raise self.body
        return {"subject": "Your claim CL-2048", "body": self.body}


def test_summary_prompt_mandates_the_three_summary_sections():
    """The drafter prompt mandates three sections: what was discussed, claim status/outcome, next steps."""
    assert "what was discussed" in EMAIL_SUMMARY_SYSTEM
    assert "claim status or outcome" in EMAIL_SUMMARY_SYSTEM
    assert "next steps" in EMAIL_SUMMARY_SYSTEM
    # The drafter is asked up front not to echo any identity value, including the address itself.
    for term in ("date of birth", "ID digits", "phone numbers", "email addresses"):
        assert term in EMAIL_SUMMARY_SYSTEM


def test_fallback_email_carries_status_and_next_steps():
    subject, body = fallback_email(_state(), CLAIM)
    assert "CL-2048" in subject
    assert "healthcare claim CL-2048" in body and "denied" in body
    assert "Next steps:" in body and "pathology report" in body and "2026-03-15" in body
    _, no_docs_body = fallback_email(_state(), {**CLAIM, "documents_needed": []})
    assert "No further action is needed" in no_docs_body


def test_clean_draft_is_used_with_no_note():
    llm = DraftLLM("We discussed claim CL-2048; the appeal deadline is 2026-03-15.")
    subject, body, note = draft_email_summary(llm, _state(), CLAIM, [])
    assert subject == "Your claim CL-2048"
    assert "2026-03-15" in body  # record dates are legitimate; only the caller's values are checked
    assert note is None


def test_model_failure_falls_back_to_template_with_note():
    llm = DraftLLM(LLMError("boom"))
    subject, body, note = draft_email_summary(llm, _state(), CLAIM, [])
    assert (subject, body) == fallback_email(_state(), CLAIM)
    assert note == EMAIL_NOTE_MODEL_FAILED


def test_draft_echoing_last4_falls_back_to_template():
    llm = DraftLLM("You verified with the last four 4472; the claim CL-2048 is denied.")
    subject, body, note = draft_email_summary(llm, _state(), CLAIM, [])
    assert (subject, body) == fallback_email(_state(), CLAIM)
    assert "4472" not in body
    assert note == EMAIL_NOTE_PII_IN_DRAFT


def test_draft_echoing_dob_falls_back_to_template():
    llm = DraftLLM("Date of birth on file: 1985-03-15.")
    _, body, note = draft_email_summary(llm, _state(), CLAIM, [])
    assert "1985-03-15" not in body
    assert note == EMAIL_NOTE_PII_IN_DRAFT


def test_draft_echoing_formatted_phone_falls_back_to_template():
    llm = DraftLLM("We will call you at (650) 521-2836 if needed.")
    _, body, note = draft_email_summary(llm, _state(), CLAIM, [])
    assert "521-2836" not in body
    assert note == EMAIL_NOTE_PII_IN_DRAFT


def test_draft_echoing_email_in_subject_falls_back_to_template():
    class SubjectLLM(DraftLLM):
        def complete_json(self, system: str, messages: list, *, max_tokens: int = 700) -> dict[str, Any]:
            return {"subject": "Summary for Margaret@Email.com", "body": "All good."}

    subject, _, note = draft_email_summary(SubjectLLM("unused"), _state(), CLAIM, [])
    assert subject == fallback_email(_state(), CLAIM)[0]
    assert "email.com" not in subject.lower()
    assert note == EMAIL_NOTE_PII_IN_DRAFT


def test_empty_slots_make_the_check_a_no_op():
    """Representative sessions never merge slots; a body mentioning digits must still pass."""
    llm = DraftLLM("Reference 4472 and deadline 1985-03-15 and phone (650) 521-2836.")
    state = SessionState(session_id="rep", verified=True, policyholder_name="Margaret Chen")
    _, body, note = draft_email_summary(llm, state, CLAIM, [])
    assert "4472" in body
    assert note is None


def test_contains_caller_pii_matches_phone_formats_and_last4_as_word():
    assert contains_caller_pii("call 650-521-2836", SLOTS)
    assert contains_caller_pii("call +16505212836", SLOTS)
    assert contains_caller_pii("call 650.521.2836", SLOTS)
    assert contains_caller_pii("last four 4472", SLOTS)
    assert not contains_caller_pii("reference 44721", SLOTS)
    assert not contains_caller_pii("claim CL-2048 for 1450.00, appeal by 2026-03-15", SLOTS)
    assert not contains_caller_pii("anything", IdentitySlots())


def test_send_path_appends_pii_note_to_decisions(verified_harness: Harness):
    """End to end: a draft echoing the caller's last-4 is replaced and the decision line is recorded."""
    harness = verified_harness
    harness.llm.scripts["that's all"] = {"conversation_done": True}
    harness.say("Ok, that's all I needed")
    assert harness.state.phase == "POST_PROCESS"

    original = harness.llm.complete_json

    def leaky(system: str, messages: list, *, max_tokens: int = 700) -> dict[str, Any]:
        if '"subject"' in system:
            harness.llm.json_calls.append("email_summary")
            return {"subject": "Your claim", "body": "You confirmed the last four 4472 with us."}
        return original(system, messages, max_tokens=max_tokens)

    harness.llm.complete_json = leaky  # type: ignore[method-assign]
    harness.llm.scripts["yes please"] = {"email_consent": "yes"}
    result = harness.say("Yes please")
    assert harness.state.phase == "CLOSED"
    assert len(harness.state.outbox) == 1
    entry = harness.state.outbox[0]
    assert "4472" not in entry.body
    assert entry.subject == fallback_email(harness.state, CLAIM)[0]
    assert result.debug is not None
    assert EMAIL_NOTE_PII_IN_DRAFT in result.debug["decisions"]


def test_send_path_has_no_note_for_a_clean_draft(verified_harness: Harness):
    harness = verified_harness
    harness.llm.scripts["that's all"] = {"conversation_done": True}
    harness.say("Ok, that's all I needed")
    harness.llm.scripts["yes please"] = {"email_consent": "yes"}
    result = harness.say("Yes please")
    assert harness.state.phase == "CLOSED"
    assert result.debug is not None
    assert not any(d.startswith("email_summary:") for d in result.debug["decisions"])
