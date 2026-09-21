"""Scripted live scenarios against a running agent (real model required for PASS).

Usage:
    python scripts/live_demo.py                       # all scenarios against http://localhost:8000
    python scripts/live_demo.py --scenario angry_caller
    python scripts/live_demo.py --base-url http://localhost:8010 --scenario demo_caller

Each scenario plays scripted caller turns through the HTTP API, prints every user/agent turn with the
SOP phase, then applies rule-based checks. Text checks are tolerant (case-insensitive substring sets,
plus a few regexes for required or forbidden phrasings); state checks are strict. Exit code is 0 when every check passes, 1 when any check fails, 2 when the
server is unreachable or the scenario name is unknown.

Dependencies: standard library + httpx (already in requirements.txt).
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

DEMO_UTTERANCE = (
    "I'm the policyholder. My name is Margaret Chen, policy POL-9921. I'm calling about my denied "
    "healthcare claim from January. DOB is 1985-03-15, SSN last four is 4472."
)
NOT_CONFIGURED_MARKER = "language model is not configured"

Check = Callable[[dict[str, Any], str], tuple[str, bool, str]]


# --------------------------------------------------------------------------------------------------
# Check builders. Each returns (name, ok, detail).
# --------------------------------------------------------------------------------------------------


def _get(state: dict[str, Any], path: str) -> Any:
    value: Any = state
    for key in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def state_eq(path: str, expected: Any) -> Check:
    def check(state: dict[str, Any], _reply: str) -> tuple[str, bool, str]:
        actual = _get(state, path)
        return f"state.{path} == {expected!r}", actual == expected, f"got {actual!r}"

    return check


def state_in(path: str, options: list[Any]) -> Check:
    def check(state: dict[str, Any], _reply: str) -> tuple[str, bool, str]:
        actual = _get(state, path)
        return f"state.{path} in {options!r}", actual in options, f"got {actual!r}"

    return check


def state_contains(path: str, needle: str) -> Check:
    """Substring match on a string field, or on any element of a list field (memory.notes)."""

    def check(state: dict[str, Any], _reply: str) -> tuple[str, bool, str]:
        actual = _get(state, path)
        haystacks = actual if isinstance(actual, list) else [actual]
        ok = any(isinstance(h, str) and needle.lower() in h.lower() for h in haystacks)
        return f"state.{path} contains {needle!r}", ok, _snippet(str(actual))

    return check


def outbox_len(expected: int) -> Check:
    def check(state: dict[str, Any], _reply: str) -> tuple[str, bool, str]:
        actual = len(state.get("outbox") or [])
        return f"len(state.outbox) == {expected}", actual == expected, f"got {actual}"

    return check


def outbox_mentions(index: int, needle: str) -> Check:
    def check(state: dict[str, Any], _reply: str) -> tuple[str, bool, str]:
        outbox = state.get("outbox") or []
        name = f"outbox[{index}] subject or body contains {needle!r}"
        if index >= len(outbox):
            return name, False, "no such outbox entry"
        entry = outbox[index]
        text = f"{entry.get('subject', '')} {entry.get('body', '')}"
        return name, needle.lower() in text.lower(), _snippet(str(entry.get("body", "")))

    return check


def reply_any(needles: list[str]) -> Check:
    def check(_state: dict[str, Any], reply: str) -> tuple[str, bool, str]:
        lowered = reply.lower()
        found = [n for n in needles if n.lower() in lowered]
        return f"reply mentions any of {needles!r}", bool(found), f"found {found!r}"

    return check


def reply_none(needles: list[str]) -> Check:
    def check(_state: dict[str, Any], reply: str) -> tuple[str, bool, str]:
        lowered = reply.lower()
        leaked = [n for n in needles if n.lower() in lowered]
        return f"reply contains none of {needles!r}", not leaked, f"leaked {leaked!r}" if leaked else "clean"

    return check


def reply_regex(pattern: str, label: str) -> Check:
    compiled = re.compile(pattern, re.IGNORECASE)

    def check(_state: dict[str, Any], reply: str) -> tuple[str, bool, str]:
        match = compiled.search(reply)
        return f"reply {label}", match is not None, f"found {match.group(0)!r}" if match else "not found"

    return check


def reply_not_regex(pattern: str, label: str) -> Check:
    compiled = re.compile(pattern, re.IGNORECASE)

    def check(_state: dict[str, Any], reply: str) -> tuple[str, bool, str]:
        match = compiled.search(reply)
        return f"reply {label}", match is None, f"found {match.group(0)!r}" if match else "clean"

    return check


def _snippet(text: str, limit: int = 80) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


# --------------------------------------------------------------------------------------------------
# Scenarios
# --------------------------------------------------------------------------------------------------


@dataclass
class Turn:
    text: str
    checks: list[Check] = field(default_factory=list)


@dataclass
class Scenario:
    name: str
    description: str
    turns: list[Turn]
    # JSON body for POST /api/session; representative scenarios pick the consent_scenario here.
    session_body: dict[str, Any] = field(default_factory=dict)


REP_UTTERANCE = (
    "Hi, this is David Chen. I'm calling on behalf of my mother, Margaret Chen, about her denied "
    "healthcare claim from January."
)
CLAIM_TERMS = ["CL-", "pathology", "office note", "denied due"]
FALSE_STATUS_TERMS = ["verified now", "got you verified", "you're verified", "you are verified", "looking at your account"]
# Generic "some details did not match" wording (a negation within a few words of "match", or "mismatch",
# or "didn't line up"); mirrors app/sop/guard.py UnstatedMismatchRule. Never names the mismatched factor.
MISMATCH_PATTERN = (
    r"(?:n't|\b(?:not|never|unable to|trouble|failed to|cannot))\b[ ,]+(?:[\w']+[ ,]+){0,3}match(?:ing|ed|es)?\b"
    r"|\bmismatch"
    r"|(?:n't|\bnot)\b[ ,]+(?:[\w']+[ ,]+){0,2}line up\b"
)
# "1 more" / "one last" as a remaining-factor count while two factors are still needed; conversational
# filler ("one more attempt") is not a count. Mirrors app/sop/guard.py WrongFactorCountRule.
ONE_MORE_PATTERN = r"\b(?:1|one)\s+(?:more|last|final|additional)\s+(?!attempt|try|chance)"
QUEUE_TERMS = ["queue", "human claims representative"]
# The responder only ever sees the redacted transcript (app/sop/redact.py); a placeholder token in a reply
# means the model echoed one instead of talking around it. Checked on the final turn of every scenario.
NO_REDACTION_TOKEN = reply_not_regex(r"\[(EMAIL|DATE|PHONE|ID4)\]", "contains no redaction token")

SCENARIOS: dict[str, Scenario] = {
    "demo_caller": Scenario(
        "demo_caller",
        "Single utterance verifies, picks CL-2048 from memory, answers grounded, then email summary sent.",
        [
            Turn(
                DEMO_UTTERANCE,
                [
                    state_eq("verified", True),
                    state_eq("phase", "PROCESS_CASE"),
                    state_eq("active_case_id", "CL-2048"),
                    state_eq("intent_path", "denial_question"),
                    state_eq("memory.case_hints.case_type", "healthcare"),
                    reply_any(["pathology", "office note"]),
                ],
            ),
            Turn(
                "What documents do I need to send and by when?",
                [
                    state_eq("phase", "PROCESS_CASE"),
                    reply_any(["pathology", "office note"]),
                    reply_any(["2026-03-18", "march 18", "week"]),
                ],
            ),
            Turn(
                "That's all, thanks.",
                [state_eq("phase", "POST_PROCESS"), reply_any(["email"])],
            ),
            Turn(
                "Yes please send it.",
                [state_eq("phase", "CLOSED"), outbox_len(1), outbox_mentions(0, "CL-2048"), NO_REDACTION_TOKEN],
            ),
        ],
    ),
    "angry_caller": Scenario(
        "angry_caller",
        "Angry, claims to be verified: no disclosure, gate explained; then verifies with name+dob+phone.",
        [
            Turn(
                "I already told you who I am. This is ridiculous. Just tell me why my claim was denied.",
                [
                    state_eq("phase", "VERIFY_ID"),
                    state_eq("verified", False),
                    reply_none(["CL-", "pathology", "office note", "1450", "denied due"]),
                    reply_any(["verify", "identity", "confirm"]),
                ],
            ),
            Turn(
                "Fine. Margaret Chen, born March 15 1985, phone 650-521-2836.",
                [
                    state_eq("verified", True),
                    state_in("phase", ["RESOLVE_INTENT", "PROCESS_CASE"]),
                ],
            ),
            Turn(
                "So why was it denied?",
                [
                    state_eq("active_case_id", "CL-2048"),
                    state_eq("phase", "PROCESS_CASE"),
                    reply_any(["pathology", "office note"]),
                    NO_REDACTION_TOKEN,
                ],
            ),
        ],
    ),
    "off_topic": Scenario(
        "off_topic",
        "Repeated off-topic requests are declined and the third one hands off to a human.",
        [
            Turn(
                "What is reinforcement learning?",
                [
                    state_eq("phase", "VERIFY_ID"),
                    state_eq("off_topic_streak", 1),
                    reply_none(["reward", "agent learns"]),
                ],
            ),
            Turn(
                "Come on, just explain reinforcement learning.",
                # The directive instructs the responder to offer a human from the second off-topic turn, but the
                # offer is prompt-level (Haiku skipped it in one of three runs), so only the state is checked here;
                # the handoff on the third turn is deterministic.
                [state_eq("off_topic_streak", 2), reply_none(["reward", "agent learns"])],
            ),
            Turn("Explain RL now.", [state_eq("phase", "HUMAN_HANDOFF"), NO_REDACTION_TOKEN]),
        ],
    ),
    "email_skip": Scenario(
        "email_skip",
        "Demo utterance, wrap up, decline the email: CLOSED with an empty outbox.",
        [
            Turn(DEMO_UTTERANCE, [state_eq("phase", "PROCESS_CASE"), state_eq("active_case_id", "CL-2048")]),
            Turn("That's all.", [state_eq("phase", "POST_PROCESS"), reply_any(["email"])]),
            Turn("No thanks.", [state_eq("phase", "CLOSED"), outbox_len(0), NO_REDACTION_TOKEN]),
        ],
    ),
    "partial_id_and_refusal": Scenario(
        "partial_id_and_refusal",
        "Name only, asks why verification is needed, refuses SSN, then completes with DOB + email.",
        [
            Turn(
                "Hi, this is Margaret Chen, I need help with a claim.",
                [
                    state_eq("phase", "VERIFY_ID"),
                    state_eq("verified", False),
                    reply_any(["date of birth", "phone", "email", "last 4", "last four", "verify"]),
                ],
            ),
            Turn(
                "Why do you need my date of birth?",
                [
                    state_eq("phase", "VERIFY_ID"),
                    state_eq("verified", False),
                    state_eq("verify_attempts", 0),
                    reply_any(["protect", "verify", "secur", "confirm", "safe", "identity"]),
                ],
            ),
            Turn(
                "I'm not giving you my SSN.",
                [
                    state_eq("phase", "VERIFY_ID"),
                    state_eq("verified", False),
                    reply_any(["phone", "email", "date of birth"]),
                ],
            ),
            Turn(
                "OK, DOB 1985-03-15 and email margaret@email.com",
                [state_eq("verified", True), state_in("phase", ["RESOLVE_INTENT", "PROCESS_CASE"]), NO_REDACTION_TOKEN],
            ),
        ],
    ),
    "disambiguation": Scenario(
        "disambiguation",
        "'Healthcare claim from January' matches two claims (CL-2048, CL-2011): the agent asks which; 'the denied one' picks CL-2048.",
        [
            Turn(
                "This is Margaret Chen, policy POL-9921, DOB 1985-03-15, SSN last four 4472. I am calling about my "
                "healthcare claim from January.",
                [
                    state_eq("verified", True),
                    state_eq("phase", "RESOLVE_INTENT"),
                    state_eq("active_case_id", None),
                    reply_regex(r"\btwo\b|\b2\b|\bboth\b|CL-2048.*CL-2011|CL-2011.*CL-2048", "names or counts both January claims"),
                    reply_none(["pathology", "office note", "denied due"]),
                ],
            ),
            Turn(
                "The denied one.",
                [
                    state_eq("phase", "PROCESS_CASE"),
                    state_eq("active_case_id", "CL-2048"),
                    reply_any(["pathology", "office note", "denied"]),
                ],
            ),
            Turn("That's all, thanks.", [state_eq("phase", "POST_PROCESS"), reply_any(["email"])]),
            Turn("No thanks.", [state_eq("phase", "CLOSED"), outbox_len(0), NO_REDACTION_TOKEN]),
        ],
    ),
    "representative_default": Scenario(
        "representative_default",
        "Son calls for his mother: authorization matched, consent pending, approved next turn, claim resolved from memory.",
        [
            Turn(
                REP_UTTERANCE,
                [
                    state_eq("phase", "VERIFY_ID"),
                    state_eq("verified", False),
                    state_eq("caller_role", "representative"),
                    state_eq("representative_name", "David Chen"),
                    state_eq("consent_status", "pending"),
                    reply_any(["consent", "permission", "approv", "authoriz"]),
                    reply_none(CLAIM_TERMS),
                ],
            ),
            Turn(
                "Okay, has she approved yet?",
                [
                    state_eq("verified", True),
                    state_eq("consent_status", "approved"),
                    state_eq("policyholder_name", "Margaret Chen"),
                    state_eq("active_case_id", "CL-2048"),
                    state_eq("phase", "PROCESS_CASE"),
                    reply_any(["approved", "approval came", "has approved", "consent"]),
                    reply_none(["just yet", "not yet", "no update", "still waiting", "still pending"]),
                ],
            ),
            Turn(
                "What does she need to send in?",
                [
                    state_eq("phase", "PROCESS_CASE"),
                    reply_any(["pathology", "office note"]),
                    reply_none(["once she approves", "still waiting", "still pending", "once we have her consent"]),
                ],
            ),
            Turn("That's all, thanks.", [state_eq("phase", "POST_PROCESS"), reply_any(["email"])]),
            Turn(
                "Yes",
                [state_eq("phase", "CLOSED"), outbox_len(1), outbox_mentions(0, "CL-2048"), NO_REDACTION_TOKEN],
            ),
        ],
        session_body={"consent_scenario": "default"},
    ),
    "representative_timeout": Scenario(
        "representative_timeout",
        "Consent never arrives: pending through five checks with no claim data, then handoff on the sixth.",
        [
            Turn(
                REP_UTTERANCE,
                [
                    state_eq("phase", "VERIFY_ID"),
                    state_eq("caller_role", "representative"),
                    state_eq("consent_status", "pending"),
                    reply_none(CLAIM_TERMS),
                ],
            ),
            Turn(
                "Okay, I'm still waiting. Has she approved?",
                [state_eq("phase", "VERIFY_ID"), state_eq("consent_status", "pending"), reply_none(CLAIM_TERMS)],
            ),
            Turn(
                "Still nothing on her end. Anything yet?",
                [state_eq("phase", "VERIFY_ID"), state_eq("consent_status", "pending"), reply_none(CLAIM_TERMS)],
            ),
            Turn(
                "I'm still waiting here.",
                [state_eq("phase", "VERIFY_ID"), state_eq("consent_status", "pending"), reply_none(CLAIM_TERMS)],
            ),
            Turn(
                "Any update on the approval?",
                [
                    state_eq("phase", "VERIFY_ID"),
                    state_eq("verified", False),
                    state_eq("consent_status", "pending"),
                    reply_none(CLAIM_TERMS),
                ],
            ),
            Turn(
                "Hello? Still waiting.",
                [
                    state_eq("phase", "HUMAN_HANDOFF"),
                    state_eq("consent_status", "timed_out"),
                    state_eq("verified", False),
                    reply_none(CLAIM_TERMS),
                    NO_REDACTION_TOKEN,
                ],
            ),
        ],
        session_body={"consent_scenario": "timeout"},
    ),
    "representative_unknown": Scenario(
        "representative_unknown",
        "Unknown representative gets a generic mismatch (no customer confirmation); correcting the name matches.",
        [
            Turn(
                "Hi, this is Bob Jones calling for my aunt Margaret Chen",
                [
                    state_eq("phase", "VERIFY_ID"),
                    state_eq("verified", False),
                    state_eq("caller_role", "representative"),
                    state_eq("verify_attempts", 1),
                    reply_none(CLAIM_TERMS),
                    reply_none(["date of birth", "last four", "last 4", "ssn"]),
                ],
            ),
            Turn(
                "Sorry, it's David Chen, her son",
                [
                    state_eq("phase", "VERIFY_ID"),
                    state_eq("representative_name", "David Chen"),
                    state_eq("consent_status", "pending"),
                    reply_none(CLAIM_TERMS),
                    NO_REDACTION_TOKEN,
                ],
            ),
        ],
        session_body={"consent_scenario": "timeout"},
    ),
    "representative_wording": Scenario(
        "representative_wording",
        "'Authorized representative' is a caller role, not a request for a human; a real human request still hands off.",
        [
            Turn(
                "Hello, I'm David Chen, Margaret Chen's authorized representative, calling about her claim",
                [
                    state_eq("phase", "VERIFY_ID"),
                    state_eq("handoff_note", None),
                    state_eq("caller_role", "representative"),
                    reply_none(CLAIM_TERMS),
                ],
            ),
            Turn(
                "Actually, can I just talk to a person?",
                [state_eq("phase", "HUMAN_HANDOFF"), NO_REDACTION_TOKEN],
            ),
        ],
        session_body={"consent_scenario": "timeout"},
    ),
    "false_verification_replay": Scenario(
        "false_verification_replay",
        "Trace 3a106799921c replay: one matched factor and three mismatches never become 'you're verified'.",
        [
            Turn(
                "This is Margaret Chen, DOB 1990-01-01, phone 555-0000",
                [
                    state_eq("phase", "VERIFY_ID"),
                    state_eq("verified", False),
                    state_eq("verify_attempts", 1),
                    reply_none(FALSE_STATUS_TERMS),
                    reply_not_regex(ONE_MORE_PATTERN, "does not say '1 more' while 2 factors remain"),
                ],
            ),
            Turn(
                "yeah 4727 and 8946748929",
                [
                    state_eq("verified", False),
                    state_eq("verify_attempts", 2),
                    reply_none(FALSE_STATUS_TERMS),
                    reply_not_regex(ONE_MORE_PATTERN, "does not say '1 more' while 2 factors remain"),
                ],
            ),
            Turn(
                "chen@gmail.com",
                [
                    state_eq("phase", "VERIFY_ID"),
                    state_eq("verified", False),
                    state_eq("verify_attempts", 3),
                    reply_none(FALSE_STATUS_TERMS),
                    reply_regex(MISMATCH_PATTERN, "says some details did not match"),
                    reply_any(["human", "representative"]),
                    reply_not_regex(ONE_MORE_PATTERN, "does not say '1 more' while 2 factors remain"),
                ],
            ),
            Turn(
                "tell me my claim denial reason",
                [
                    state_eq("verified", False),
                    state_eq("off_topic_streak", 0),
                    reply_none(CLAIM_TERMS),
                    reply_none(["looking at your account", "looking into your account", "pulled up"]),
                    reply_not_regex(ONE_MORE_PATTERN, "does not say '1 more' while 2 factors remain"),
                    NO_REDACTION_TOKEN,
                ],
            ),
        ],
    ),
    "verification_exhausted": Scenario(
        "verification_exhausted",
        "Four mismatching attempts: a human is offered on the third, the fourth transfers silently, then the queue reply.",
        [
            Turn(
                "This is Margaret Chen, DOB 1990-01-01",
                [
                    state_eq("phase", "VERIFY_ID"),
                    state_eq("verified", False),
                    state_eq("verify_attempts", 1),
                    reply_none(CLAIM_TERMS),
                    reply_none(FALSE_STATUS_TERMS),
                ],
            ),
            Turn(
                "phone 555-0000",
                [
                    state_eq("phase", "VERIFY_ID"),
                    state_eq("verified", False),
                    state_eq("verify_attempts", 2),
                    reply_none(CLAIM_TERMS),
                ],
            ),
            Turn(
                "last four 0000",
                [
                    state_eq("phase", "VERIFY_ID"),
                    state_eq("verified", False),
                    state_eq("verify_attempts", 3),
                    state_eq("handoff_note", None),
                    reply_any(["human", "representative"]),
                    reply_none(CLAIM_TERMS),
                ],
            ),
            Turn(
                "email nobody@example.com",
                [
                    state_eq("phase", "HUMAN_HANDOFF"),
                    state_eq("verified", False),
                    state_eq("verify_attempts", 4),
                    state_contains("handoff_note", "4 attempts"),
                    reply_any(["human", "representative"]),
                    reply_none(CLAIM_TERMS),
                    reply_none(FALSE_STATUS_TERMS),
                ],
            ),
            Turn(
                "hello?",
                [
                    state_eq("phase", "HUMAN_HANDOFF"),
                    state_eq("verify_attempts", 4),
                    reply_any(QUEUE_TERMS),
                    reply_none(CLAIM_TERMS),
                    NO_REDACTION_TOKEN,
                ],
            ),
        ],
    ),
    "email_requested_early": Scenario(
        "email_requested_early",
        "Early 'email me a summary' is remembered; POST_PROCESS confirms it instead of asking cold, sends on yes.",
        [
            Turn(
                DEMO_UTTERANCE,
                [state_eq("verified", True), state_eq("phase", "PROCESS_CASE"), state_eq("active_case_id", "CL-2048")],
            ),
            Turn(
                "Can you email me a summary when we're done?",
                [
                    state_eq("phase", "PROCESS_CASE"),
                    state_eq("memory.email_preference", "yes"),
                    state_contains("memory.notes", "caller asked for an emailed summary"),
                    outbox_len(0),
                ],
            ),
            Turn(
                "What documents do I need to send?",
                [state_eq("phase", "PROCESS_CASE"), reply_any(["pathology", "office note"])],
            ),
            Turn(
                "That's all, thanks.",
                [
                    state_eq("phase", "POST_PROCESS"),
                    reply_any(["m*****@"]),
                    reply_regex(r"earlier|mentioned|asked|requested|as you", "recalls the earlier email request"),
                    reply_regex(r"\?", "asks for the yes before sending"),
                    outbox_len(0),
                ],
            ),
            Turn(
                "Yes please.",
                [state_eq("phase", "CLOSED"), outbox_len(1), outbox_mentions(0, "CL-2048"), NO_REDACTION_TOKEN],
            ),
        ],
    ),
}


# --------------------------------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------------------------------


class AgentClient:
    def __init__(self, base_url: str, timeout: float):
        self._http = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout)

    def healthz(self) -> dict[str, Any]:
        response = self._http.get("/healthz")
        response.raise_for_status()
        return response.json()

    def new_session(self, body: dict[str, Any] | None = None) -> tuple[str, str]:
        response = self._http.post("/api/session", json=body or {})
        response.raise_for_status()
        data = response.json()
        return data["session_id"], data["reply"]

    def say(self, session_id: str, message: str) -> dict[str, Any]:
        response = self._http.post(f"/api/session/{session_id}/message", json={"message": message})
        response.raise_for_status()
        return response.json()


def run_scenario(client: AgentClient, scenario: Scenario, verbose: bool) -> tuple[int, int, bool]:
    """Returns (passed, failed, model_not_configured)."""
    print()
    print("=" * 88)
    print(f"SCENARIO {scenario.name}: {scenario.description}")
    print("=" * 88)
    session_id, greeting = client.new_session(scenario.session_body)
    print(f"[VERIFY_ID] AGENT: {greeting}")
    passed = failed = 0
    not_configured = False

    for index, turn in enumerate(scenario.turns, start=1):
        print()
        print(f"--- turn {index}")
        print(f"USER : {turn.text}")
        data = client.say(session_id, turn.text)
        reply, state = data["reply"], data["state"]
        print(f"AGENT [{state['phase']}]: {reply}")
        if NOT_CONFIGURED_MARKER in reply:
            not_configured = True
        if verbose and data.get("debug"):
            for decision in data["debug"].get("decisions", []):
                print(f"      decision: {decision}")
        for check in turn.checks:
            name, ok, detail = check(state, reply)
            print(f"  {'PASS' if ok else 'FAIL'}  {name}  ({detail})")
            if ok:
                passed += 1
            else:
                failed += 1

    return passed, failed, not_configured


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default="http://localhost:8000", help="agent base URL (default: %(default)s)")
    parser.add_argument(
        "--scenario",
        default="all",
        choices=["all", *SCENARIOS],
        help="scenario to run (default: all)",
    )
    parser.add_argument("--timeout", type=float, default=90.0, help="per-request timeout in seconds")
    parser.add_argument("--verbose", action="store_true", help="print controller decisions from the debug trace")
    args = parser.parse_args(argv)

    client = AgentClient(args.base_url, args.timeout)
    try:
        health = client.healthz()
    except httpx.HTTPError as exc:
        print(f"Cannot reach {args.base_url}: {exc}", file=sys.stderr)
        print("Start the server first: uvicorn app.main:app --port 8000", file=sys.stderr)
        return 2
    print(f"Server {args.base_url}: provider={health.get('provider')} model={health.get('model')}")

    selected = list(SCENARIOS.values()) if args.scenario == "all" else [SCENARIOS[args.scenario]]
    totals: list[tuple[str, int, int]] = []
    any_not_configured = False
    for scenario in selected:
        try:
            passed, failed, not_configured = run_scenario(client, scenario, args.verbose)
        except httpx.HTTPError as exc:
            print(f"  ERROR request failed during scenario {scenario.name}: {exc}", file=sys.stderr)
            passed, failed, not_configured = 0, len([c for t in scenario.turns for c in t.checks]), False
        totals.append((scenario.name, passed, failed))
        any_not_configured = any_not_configured or not_configured

    print()
    print("=" * 88)
    print("SUMMARY")
    print("=" * 88)
    total_failed = 0
    for name, passed, failed in totals:
        total_failed += failed
        print(f"  {'PASS' if failed == 0 else 'FAIL'}  {name:<26} {passed} passed, {failed} failed")
    if any_not_configured:
        print()
        print("NOTE: the server replied with the 'model not configured' notice. Set LLM_API_KEY in .env")
        print("      (see .env.example), restart the server and run this script again.")
    print()
    print("RESULT:", "ALL CHECKS PASSED" if total_failed == 0 else f"{total_failed} CHECK(S) FAILED")
    return 0 if total_failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
