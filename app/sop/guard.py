"""Output guard: a small rule engine that scans drafted replies before they reach the caller.

Each rule owns three things: when it applies (a predicate on the session state and this turn's
directive), what it scans for, and the correction sentence the responder receives if it fires. A
rule may forbid content (claim data, false status), require it (the mismatch statement the
directive mandates) or compare the reply against the directive (the remaining-factor count); all
produce Violations. The controller only sees Violations; it never knows which rules exist or what
they look for.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from app.sop.prompts import GUARD_CORRECTIONS
from app.sop.state import Directive, SessionState
from app.tools.data import FixtureStore

_MIN_TERM_LENGTH = 4


class GuardRule(Protocol):
    name: str
    correction: str

    def applies(self, state: SessionState, directive: Directive) -> bool: ...

    def scan(self, text: str, directive: Directive) -> list[str]: ...


@dataclass
class Violation:
    rule: str
    hits: list[str]
    correction: str


class ClaimLeakRule:
    """No claim facts before verification: fixture-derived strings, case ids and amounts."""

    name = "claim_leak"
    correction = GUARD_CORRECTIONS["claim_leak"]

    def __init__(self, store: FixtureStore):
        self._terms = [t.lower() for t in store.sensitive_terms() if len(t) >= _MIN_TERM_LENGTH]
        patterns = [r"\bCL-\d+\b", r"\$\s?\d[\d,]*(?:\.\d{2})?"]
        amounts = store.sensitive_amounts()
        if amounts:
            patterns.append(r"\b(?:" + "|".join(re.escape(a) for a in amounts) + r")(?:\.\d{2})?\b")
        self._patterns = [re.compile(p, re.IGNORECASE) for p in patterns]

    def applies(self, state: SessionState, directive: Directive) -> bool:
        return not state.verified

    def scan(self, text: str, directive: Directive) -> list[str]:
        """Return the sensitive terms or pattern matches found in the reply."""
        lowered = text.lower()
        hits = [term for term in self._terms if term in lowered]
        for pattern in self._patterns:
            hits.extend(match.group(0) for match in pattern.finditer(text))
        return sorted(set(hits))


# How far back (in characters) a clause is examined for a negating or conditional cue.
_CLAUSE_WINDOW = 60
_CLAUSE_BREAK = re.compile(r"[.!?]")


class FalseStatusRule:
    """No false verification status before verification: neither "you're verified" / "looking at your
    account" (completed state) nor "finishing up the verification on my end" (a process that does not
    exist; the controller verifies instantly from the factors it has).

    A match is dropped when the clause it sits in (text since the last sentence break, capped at
    ``_CLAUSE_WINDOW`` characters) contains a conditional or negating cue, so "once I've verified your
    identity" and "I haven't got you verified yet" pass while "Thanks, that's not a problem. You're
    verified now." is still caught.
    """

    name = "false_status"
    correction = GUARD_CORRECTIONS["false_status"]

    _PATTERNS = [
        re.compile(p, re.IGNORECASE)
        for p in (
            # Completed state.
            r"\b(?:got|have|has) you verified\b",
            r"\byou(?:'re| are)(?: now)?(?: fully)? verified\b",
            r"\byou(?:'ve| have) been verified\b",
            r"\bverified now\b",
            r"\b(?:your )?identity (?:is|has been|was) (?:now )?(?:confirmed|verified)\b",
            r"\b(?:confirmed|verified) your identity\b",
            r"\b(?:looking|pulling|pulled|bringing) (?:at|up|into) your (?:account|records?|file|claim)\b",
            r"\b(?:got|have) your (?:claim|records?|file) (?:open|here|up|in front of me)\b",
            # Verification claimed to be running or done on the agent's side.
            r"\bverif(?:y|ied|ying|ication)\b[^.!?]{0,40}?\bon (?:my|our) end\b",
            r"\b(?:finishing up|finalizing|finalising|processing|running|completing) (?:the |your )?verification\b",
            r"\b(?:have|got) (?:all (?:the |of the )?(?:information|details|info)|everything)(?: (?:I|we) need| now)\b",
        )
    ]
    # "need to" is deliberately absent: "I have what I need to complete verification on my end" is a false
    # progress claim, and no legitimate phrasing that reaches a pattern depends on it.
    _EXCLUSION_CUES = re.compile(
        r"\b(?:once|after|when|until|before|so I can|to get|not|cannot|unable|yet)\b|n't\b",
        re.IGNORECASE,
    )

    def applies(self, state: SessionState, directive: Directive) -> bool:
        return not state.verified

    def scan(self, text: str, directive: Directive) -> list[str]:
        """Return the completed-state phrases found outside a negated or conditional clause."""
        hits: set[str] = set()
        for pattern in self._PATTERNS:
            for match in pattern.finditer(text):
                if not self._EXCLUSION_CUES.search(self._clause_before(text, match.start())):
                    hits.add(match.group(0))
        return sorted(hits)

    @staticmethod
    def _clause_before(text: str, index: int) -> str:
        window = text[max(0, index - _CLAUSE_WINDOW) : index]
        breaks = list(_CLAUSE_BREAK.finditer(window))
        return window[breaks[-1].end() :] if breaks else window


class UnstatedMismatchRule:
    """On a VERIFY_ID turn where details did not match, the reply must say so.

    Left unsaid, the caller (and the model on later turns, reading its own transcript) assumes the
    details were accepted; that assumption is where "you're verified" replies come from. The hit is a
    description, not text from the reply, because the violation is an absence.
    """

    name = "unstated_mismatch"
    correction = GUARD_CORRECTIONS["unstated_mismatch"]
    _STATED = re.compile(
        r"(?:n't|\b(?:not|never|unable to|trouble|failed to|cannot))\b[ ,]+(?:[\w']+[ ,]+){0,3}match(?:ing|ed|es)?\b"
        r"|\bmismatch"
        r"|(?:n't|\bnot)\b[ ,]+(?:[\w']+[ ,]+){0,2}line up\b",
        re.IGNORECASE,
    )

    def applies(self, state: SessionState, directive: Directive) -> bool:
        return directive.phase == "VERIFY_ID" and bool(directive.facts.get("mismatch_this_turn"))

    def scan(self, text: str, directive: Directive) -> list[str]:
        return [] if self._STATED.search(text) else ["mismatch not stated"]


class MissingHumanOfferRule:
    """When the directive says to offer a human representative, the reply must contain the offer."""

    name = "missing_human_offer"
    correction = GUARD_CORRECTIONS["missing_human_offer"]
    _OFFERED = re.compile(r"\b(?:human|representative)\b", re.IGNORECASE)

    def applies(self, state: SessionState, directive: Directive) -> bool:
        return directive.phase == "VERIFY_ID" and bool(directive.facts.get("offer_human"))

    def scan(self, text: str, directive: Directive) -> list[str]:
        return [] if self._OFFERED.search(text) else ["human representative not offered"]


_COUNT_WORDS = {"one": 1, "two": 2, "three": 3, "1": 1, "2": 2, "3": 3}
_NUMERAL = r"(one|two|three|1|2|3)"


class WrongFactorCountRule:
    """On a located VERIFY_ID turn, any remaining-factor count the reply states must equal
    ``facts.factors_still_needed_count``.

    The model reads its own transcript and tends to count down ("I still need 1 more") after a turn
    on which nothing was accepted. The first rule that compares the reply to the directive
    numerically: every stated count is extracted and each one that differs is reported as
    ``"stated N, expected M"``. A count is not required; a reply with no number passes.

    Candidates are dropped when the two words after the numeral are conversational filler
    ("one more thing", "give it one more try", "one more attempt before I transfer you"), and the
    need-pattern ignores "any one of these".
    """

    name = "wrong_factor_count"
    correction = GUARD_CORRECTIONS["wrong_factor_count"]

    _PATTERNS = [
        re.compile(p, re.IGNORECASE)
        for p in (
            # "1 more piece", "one last detail", "two remaining".
            rf"\b{_NUMERAL}\s+(?:more|additional|further|remaining|last|final)\b",
            # "I still need 1", "need just one of the following"; not "need any one of these".
            rf"\bneed(?:s|ed)?\s+(?:just\s+|only\s+|at least\s+)?(?<!\bany\s){_NUMERAL}\b",
            # "2 pieces of information", "one detail".
            rf"\b{_NUMERAL}\s+(?:more\s+)?(?:piece|detail|item|factor|thing)s?\b",
        )
    ]
    _FILLER = re.compile(r"\b(?:attempt|try|tries|chance|time|thing|question|minute|moment)s?\b", re.IGNORECASE)
    _WORD = re.compile(r"[\w']+")

    def applies(self, state: SessionState, directive: Directive) -> bool:
        facts = directive.facts
        return (
            directive.phase == "VERIFY_ID"
            and bool(facts.get("account_located"))
            and facts.get("factors_still_needed_count") is not None
        )

    def scan(self, text: str, directive: Directive) -> list[str]:
        """Return one ``"stated N, expected M"`` per distinct count that differs from the directive."""
        expected = int(directive.facts["factors_still_needed_count"])
        stated: set[int] = set()
        for pattern in self._PATTERNS:
            for match in pattern.finditer(text):
                following = self._WORD.findall(text[match.end(1) :])[:2]
                if self._FILLER.search(" ".join(following)):
                    continue
                stated.add(_COUNT_WORDS[match.group(1).lower()])
        return [f"stated {count}, expected {expected}" for count in sorted(stated) if count != expected]


class OutputGuard:
    def __init__(self, rules: list[GuardRule]):
        self._rules = list(rules)

    @classmethod
    def default(cls, store: FixtureStore) -> OutputGuard:
        return cls(
            [
                ClaimLeakRule(store),
                FalseStatusRule(),
                UnstatedMismatchRule(),
                MissingHumanOfferRule(),
                WrongFactorCountRule(),
            ]
        )

    def applicable(self, state: SessionState, directive: Directive) -> bool:
        """True when at least one rule applies to this turn (drives the ``checked`` debug flag)."""
        return any(rule.applies(state, directive) for rule in self._rules)

    def check(self, text: str, state: SessionState, directive: Directive) -> list[Violation]:
        """Run every applicable rule; one Violation per rule that found something."""
        violations: list[Violation] = []
        for rule in self._rules:
            if not rule.applies(state, directive):
                continue
            hits = rule.scan(text, directive)
            if hits:
                violations.append(Violation(rule=rule.name, hits=hits, correction=rule.correction))
        return violations
