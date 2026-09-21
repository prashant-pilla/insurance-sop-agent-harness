from typing import Callable

from app.sop.phases.post_process import ENTRY_OFFER_INSTRUCTIONS
from app.sop.prompts import EMAIL_PREFERENCE_INSTRUCTION
from tests.conftest import DEMO_EXTRACTION, DEMO_UTTERANCE, Harness

ASKED_NOTE = "caller asked for an emailed summary"
DECLINED_NOTE = "caller declined an emailed summary"


def _wrap_up(harness: Harness) -> None:
    harness.llm.scripts["that's all"] = {"conversation_done": True}
    result = harness.say("Ok, that's all I needed")
    assert harness.state.phase == "POST_PROCESS"
    assert "m*****@email.com" in result.reply
    assert "margaret@email.com" not in result.reply


def test_email_yes_sends_summary_and_closes(verified_harness: Harness):
    _wrap_up(verified_harness)
    verified_harness.llm.scripts["yes please"] = {"email_consent": "yes"}
    result = verified_harness.say("Yes please")
    assert verified_harness.state.phase == "CLOSED"
    assert len(verified_harness.state.outbox) == 1
    entry = verified_harness.state.outbox[0]
    assert entry.to_masked == "m*****@email.com"
    assert entry.subject and entry.body and entry.sent_at
    assert "email_summary" in verified_harness.llm.json_calls
    assert result.debug is not None
    assert any(c["role"] == "email_summary" for c in result.debug["llm_calls"])


def test_email_no_closes_without_sending(verified_harness: Harness):
    _wrap_up(verified_harness)
    verified_harness.llm.scripts["no thanks"] = {"email_consent": "no"}
    verified_harness.say("No thanks")
    assert verified_harness.state.phase == "CLOSED"
    assert verified_harness.state.outbox == []


def test_different_address_is_declined(verified_harness: Harness):
    _wrap_up(verified_harness)
    verified_harness.llm.scripts["gmail"] = {"email_consent": "yes", "email": "someone.else@gmail.com"}
    result = verified_harness.say("Yes, send it to someone.else@gmail.com")
    assert verified_harness.state.phase == "POST_PROCESS"
    assert verified_harness.state.outbox == []
    assert result.debug is not None
    assert result.debug["decisions"][-1].startswith("POST_PROCESS: declined caller-supplied address")
    assert "someone.else@gmail.com" not in result.debug["directive"]["facts"].values()

    verified_harness.llm.scripts["on file"] = {"email_consent": "yes"}
    verified_harness.say("Ok, the one on file is fine")
    assert verified_harness.state.phase == "CLOSED"
    assert len(verified_harness.state.outbox) == 1


def test_no_email_on_file_closes_without_offer_instead_of_crashing(verified_harness: Harness):
    """A policyholder record without an email cannot receive a summary: close, do not 500."""
    store = verified_harness.controller._store  # noqa: SLF001 (test reaches into the fixture store)
    holder = store.get_policyholder(verified_harness.state.party_id or "")
    assert holder is not None
    saved = holder.pop("email")
    try:
        verified_harness.llm.scripts["that's all"] = {"conversation_done": True}
        result = verified_harness.say("Ok, that's all I needed")
        assert verified_harness.state.phase == "CLOSED"
        assert verified_harness.state.outbox == []
        assert result.debug is not None
        assert "POST_PROCESS: no email on file -> CLOSED without offer" in result.debug["decisions"]
        assert "@" not in result.reply
        # The extractor-failure fallback for POST_PROCESS tolerates the same record.
        verified_harness.state.phase = "POST_PROCESS"
        assert "your address on file" in verified_harness.controller._phase_fallback(verified_harness.state)  # noqa: SLF001
    finally:
        holder["email"] = saved


def test_unclear_consent_reasks_once_then_closes(verified_harness: Harness):
    _wrap_up(verified_harness)
    verified_harness.llm.scripts["why"] = {"email_consent": "unclear"}
    verified_harness.say("Why do you need to ask?")
    assert verified_harness.state.phase == "POST_PROCESS"
    verified_harness.say("Why though")
    assert verified_harness.state.phase == "CLOSED"
    assert verified_harness.state.outbox == []


# --- Email preference remembered across phases -------------------------------------------------------


def test_default_entry_offer_has_no_preference(verified_harness: Harness):
    _wrap_up(verified_harness)
    debug = verified_harness.state.last_debug
    assert debug is not None
    assert verified_harness.state.memory.email_preference is None
    assert debug["decisions"][-1] == "POST_PROCESS: offering email summary"
    assert debug["directive"]["instructions"][0] == ENTRY_OFFER_INSTRUCTIONS[None]


def test_email_request_during_verify_id_is_remembered(make_harness: Callable[..., Harness]):
    harness = make_harness(
        {
            "email me a summary": {"email_consent": "yes", "intent_hints": ["wants an emailed summary"]},
            "margaret chen, policy pol-9921": DEMO_EXTRACTION,
        }
    )
    harness.say("Hi, please email me a summary when we're done.")
    assert harness.state.phase == "VERIFY_ID"
    assert harness.state.memory.email_preference == "yes"
    assert harness.state.memory.notes == [ASKED_NOTE]
    assert harness.state.outbox == []

    harness.say(DEMO_UTTERANCE)
    assert harness.state.phase == "PROCESS_CASE"
    assert harness.state.memory.email_preference == "yes"
    # The remembered preference is exposed to the responder like any other memory note.
    caller_context = harness.state.last_debug["directive"]["facts"]["caller_context"]  # type: ignore[index]
    assert ASKED_NOTE in caller_context["notes"]


def test_email_request_during_process_case_is_remembered_once(verified_harness: Harness):
    verified_harness.llm.scripts["summary afterwards"] = {"email_consent": "yes"}
    verified_harness.say("Can I get a summary afterwards by email?")
    verified_harness.say("Yes, a summary afterwards please")
    assert verified_harness.state.phase == "PROCESS_CASE"
    assert verified_harness.state.memory.email_preference == "yes"
    assert verified_harness.state.memory.notes.count(ASKED_NOTE) == 1
    assert verified_harness.state.outbox == []


def test_early_email_request_instructs_no_follow_up_promise(make_harness: Callable[..., Harness]):
    """Before POST_PROCESS the responder is told to acknowledge the request and never promise a follow-up.

    Pins the fix for a live transcript in which the agent said "a representative will follow up".
    """
    harness = make_harness(
        {
            "email me a summary": {"email_consent": "yes", "intent_hints": ["wants an emailed summary"]},
            "margaret chen, policy pol-9921": DEMO_EXTRACTION,
            "summary afterwards": {"email_consent": "yes"},
            "that's all": {"conversation_done": True},
        }
    )
    harness.say("Hi, please email me a summary when we're done.")
    assert harness.state.phase == "VERIFY_ID"
    assert EMAIL_PREFERENCE_INSTRUCTION in harness.state.last_debug["directive"]["instructions"]  # type: ignore[index]
    assert "follow up" in EMAIL_PREFERENCE_INSTRUCTION and "Never say" in EMAIL_PREFERENCE_INSTRUCTION

    harness.say(DEMO_UTTERANCE)
    assert harness.state.phase == "PROCESS_CASE"
    assert EMAIL_PREFERENCE_INSTRUCTION in harness.state.last_debug["directive"]["instructions"]  # type: ignore[index]

    harness.say("Can I get a summary afterwards by email?")
    assert harness.state.phase == "PROCESS_CASE"
    assert EMAIL_PREFERENCE_INSTRUCTION in harness.state.last_debug["directive"]["instructions"]  # type: ignore[index]
    assert harness.state.outbox == []

    # POST_PROCESS owns the offer wording; the pre-offer instruction is not stacked on top of it.
    harness.say("Ok, that's all I needed")
    assert harness.state.phase == "POST_PROCESS"
    instructions = harness.state.last_debug["directive"]["instructions"]  # type: ignore[index]
    assert instructions[0] == ENTRY_OFFER_INSTRUCTIONS["yes"]
    assert EMAIL_PREFERENCE_INSTRUCTION not in instructions


def test_no_email_preference_adds_no_pre_offer_instruction(verified_harness: Harness):
    verified_harness.llm.scripts["don't email"] = {"email_consent": "no"}
    verified_harness.say("Please don't email me anything about this")
    assert verified_harness.state.memory.email_preference == "no"
    assert EMAIL_PREFERENCE_INSTRUCTION not in verified_harness.state.last_debug["directive"]["instructions"]  # type: ignore[index]


def test_yes_preference_changes_entry_offer_but_still_needs_a_yes(verified_harness: Harness):
    verified_harness.llm.scripts["summary afterwards"] = {"email_consent": "yes"}
    verified_harness.say("Can I get a summary afterwards by email?")
    _wrap_up(verified_harness)
    debug = verified_harness.state.last_debug
    assert debug is not None
    assert debug["decisions"][-1] == "POST_PROCESS: offering email summary (remembered preference: yes)"
    assert debug["directive"]["instructions"][0] == ENTRY_OFFER_INSTRUCTIONS["yes"]
    assert debug["directive"]["facts"]["masked_email_on_file"] == "m*****@email.com"
    # Consent stays contemporaneous: nothing is sent until the caller confirms in POST_PROCESS.
    assert verified_harness.state.outbox == []
    assert verified_harness.state.email_offers == 1

    verified_harness.llm.scripts["yes please"] = {"email_consent": "yes"}
    verified_harness.say("Yes please")
    assert verified_harness.state.phase == "CLOSED"
    assert len(verified_harness.state.outbox) == 1
    assert verified_harness.state.outbox[0].to_masked == "m*****@email.com"


def test_yes_preference_then_no_on_the_turn_closes_without_email(verified_harness: Harness):
    verified_harness.llm.scripts["summary afterwards"] = {"email_consent": "yes"}
    verified_harness.say("Can I get a summary afterwards by email?")
    _wrap_up(verified_harness)
    verified_harness.llm.scripts["changed my mind"] = {"email_consent": "no"}
    verified_harness.say("Actually I changed my mind, no email")
    assert verified_harness.state.phase == "CLOSED"
    assert verified_harness.state.outbox == []
    # A reply inside POST_PROCESS is consent for that offer, not a preference change.
    assert verified_harness.state.memory.email_preference == "yes"


def test_no_preference_still_offers_once(verified_harness: Harness):
    verified_harness.llm.scripts["don't email"] = {"email_consent": "no"}
    verified_harness.say("Please don't email me anything about this")
    assert verified_harness.state.phase == "PROCESS_CASE"
    assert verified_harness.state.memory.email_preference == "no"
    assert verified_harness.state.memory.notes == [DECLINED_NOTE]

    _wrap_up(verified_harness)
    debug = verified_harness.state.last_debug
    assert debug is not None
    assert debug["decisions"][-1] == "POST_PROCESS: offering email summary (remembered preference: no)"
    assert debug["directive"]["instructions"][0] == ENTRY_OFFER_INSTRUCTIONS["no"]
    assert verified_harness.state.outbox == []

    verified_harness.llm.scripts["still no"] = {"email_consent": "no"}
    verified_harness.say("Still no, thanks")
    assert verified_harness.state.phase == "CLOSED"
    assert verified_harness.state.outbox == []


def test_preference_flip_replaces_the_stale_note(verified_harness: Harness):
    verified_harness.llm.scripts["don't email"] = {"email_consent": "no"}
    verified_harness.llm.scripts["do email"] = {"email_consent": "yes"}
    verified_harness.say("Please don't email me anything about this")
    verified_harness.say("Actually, do email me a summary")
    assert verified_harness.state.memory.email_preference == "yes"
    assert verified_harness.state.memory.notes == [ASKED_NOTE]


def test_same_turn_request_sends_directly(verified_harness: Harness):
    verified_harness.llm.scripts["email me"] = {"conversation_done": True, "email_consent": "yes"}
    verified_harness.say("That's all, email me the summary please")
    assert verified_harness.state.phase == "CLOSED"
    assert len(verified_harness.state.outbox) == 1
    assert verified_harness.state.memory.email_preference == "yes"
