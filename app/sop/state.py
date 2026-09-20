"""Session state, memory, extractor output and directive models."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

Phase = Literal["VERIFY_ID", "RESOLVE_INTENT", "PROCESS_CASE", "POST_PROCESS", "CLOSED", "HUMAN_HANDOFF"]
IntentPath = Literal[
    "denial_question",
    "status_inquiry",
    "document_submission",
    "next_steps",
    "general_claim_question",
    "appeal",
]
Scope = Literal["in_scope_claim", "in_scope_general", "out_of_scope", "small_talk"]
Emotion = Literal["neutral", "frustrated", "angry", "anxious", "confused", "refusing"]
EmailConsent = Literal["yes", "no", "unclear"]
CallerRole = Literal["policyholder", "representative"]
ConsentStatus = Literal["pending", "approved", "timed_out"]

CALLER_ROLES: frozenset[str] = frozenset(CallerRole.__args__)  # type: ignore[attr-defined]

NEGATIVE_EMOTIONS = {"frustrated", "angry", "refusing"}

FACTORS: tuple[str, ...] = ("name", "dob", "phone", "email", "id_last4")
FACTOR_LABELS = {
    "name": "full name",
    "dob": "date of birth",
    "phone": "phone number on file",
    "email": "email address on file",
    "id_last4": "last 4 of your SSN or government ID",
}
VERIFY_THRESHOLD = 3


class IdentitySlots(BaseModel):
    """Raw identity values accumulated across turns. Internal only, never exposed."""

    name: str | None = None
    dob: str | None = None
    phone: str | None = None
    email: str | None = None
    id_last4: str | None = None
    policy_number: str | None = None

    def merge(self, other: "IdentitySlots") -> None:
        for field, value in other.model_dump().items():
            if value:
                setattr(self, field, value)

    def provided(self) -> list[str]:
        return [field for field, value in self.model_dump().items() if value]


class CaseHints(BaseModel):
    case_type: str | None = None
    status: str | None = None
    time_hint: str | None = None
    case_id: str | None = None

    def merge(self, other: "CaseHints") -> None:
        for field, value in other.model_dump().items():
            if value:
                setattr(self, field, value)

    def any(self) -> bool:
        return any(self.model_dump().values())


class Memory(BaseModel):
    intent_hints: list[str] = Field(default_factory=list)
    case_hints: CaseHints = Field(default_factory=CaseHints)
    notes: list[str] = Field(default_factory=list)
    # Set when the caller mentions an emailed summary before POST_PROCESS; the offer there confirms it.
    email_preference: EmailConsent | None = None


def _lower(value: Any) -> Any:
    return value.strip().lower() if isinstance(value, str) else value


class Extraction(BaseModel):
    """Strict schema for the extractor LLM output."""

    model_config = ConfigDict(extra="ignore")

    name: str | None = None
    dob: str | None = None
    phone: str | None = None
    email: str | None = None
    id_last4: str | None = None
    policy_number: str | None = None
    intent_hints: list[str] = Field(default_factory=list)
    intent_path: IntentPath | None = None
    case_hints: CaseHints = Field(default_factory=CaseHints)
    scope: Scope = "in_scope_claim"
    emotion: Emotion = "neutral"
    wants_human: bool = False
    conversation_done: bool = False
    email_consent: EmailConsent = "unclear"
    # Representative flow (all optional; absent in legacy extractor output).
    caller_role: CallerRole | None = None
    representative_name: str | None = None
    relationship: str | None = None
    on_behalf_of_name: str | None = None

    @field_validator("caller_role", mode="before")
    @classmethod
    def _lenient_role(cls, value: Any) -> Any:
        """Unknown role values become None instead of failing the whole extraction."""
        value = _lower(value)
        return value if value in CALLER_ROLES else None

    @field_validator("intent_path", "scope", "emotion", "email_consent", mode="before")
    @classmethod
    def _normalize_enum(cls, value: Any, info: ValidationInfo) -> Any:
        value = _lower(value)
        if value in (None, "", "null", "none"):
            return cls.model_fields[info.field_name].default if info.field_name else None
        return value

    @field_validator(
        "name",
        "dob",
        "phone",
        "email",
        "id_last4",
        "policy_number",
        "representative_name",
        "relationship",
        "on_behalf_of_name",
        mode="before",
    )
    @classmethod
    def _blank_to_none(cls, value: Any) -> Any:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @field_validator("intent_hints", mode="before")
    @classmethod
    def _hints_list(cls, value: Any) -> Any:
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        return value

    def slots(self) -> IdentitySlots:
        return IdentitySlots(
            name=self.name,
            dob=self.dob,
            phone=self.phone,
            email=self.email,
            id_last4=self.id_last4,
            policy_number=self.policy_number,
        )


class OutboxEntry(BaseModel):
    to_masked: str
    subject: str
    body: str
    sent_at: str


class TranscriptTurn(BaseModel):
    role: Literal["user", "agent"]
    text: str


class RepresentativeInfo(BaseModel):
    """Who is calling on the policyholder's behalf. Set only inside the representative sub-flow."""

    name: str
    relationship: str | None = None
    on_behalf_of: str | None = None
    party_id: str | None = None


class Directive(BaseModel):
    """What the responder is allowed to say this turn. Facts are the only source of truth."""

    phase: Phase
    goal: str
    facts: dict[str, Any] = Field(default_factory=dict)
    must_ask: str | None = None
    tone: str = ""
    instructions: list[str] = Field(default_factory=list)
    forbidden: list[str] = Field(default_factory=list)
    fallback_text: str


class SessionState(BaseModel):
    session_id: str
    phase: Phase = "VERIFY_ID"
    verified: bool = False
    verified_factors: dict[str, bool] = Field(default_factory=lambda: {f: False for f in FACTORS})
    party_id: str | None = None
    policyholder_name: str | None = None
    slots: IdentitySlots = Field(default_factory=IdentitySlots)
    memory: Memory = Field(default_factory=Memory)
    active_case_id: str | None = None
    intent_path: str | None = None
    verify_attempts: int = 0
    off_topic_streak: int = 0
    frustration_streak: int = 0
    last_emotion: str | None = None
    outbox: list[OutboxEntry] = Field(default_factory=list)
    handoff_note: str | None = None
    email_offers: int = 0
    transcript: list[TranscriptTurn] = Field(default_factory=list)
    turn_count: int = 0
    last_debug: dict[str, Any] | None = None  # debug of the most recent turn (None when tracing is off)
    # Representative flow; defaults describe a normal policyholder caller.
    caller_role: CallerRole = "policyholder"
    representative: RepresentativeInfo | None = None
    consent_status: ConsentStatus | None = None
    consent_checks: int = 0
    consent_scenario: str = "default"

    def public_state(self) -> dict[str, Any]:
        """The API-facing state: never contains raw PII values.

        The representative keys (caller_role, representative_name, representative_relationship,
        consent_status) are only present once the caller has been identified as a representative, so a
        normal policyholder session keeps exactly the historical key set.
        """
        public: dict[str, Any] = {
            "phase": self.phase,
            "verified": self.verified,
            "verified_factors": {f: bool(self.verified_factors.get(f)) for f in FACTORS},
            "policyholder_name": self.policyholder_name if self.verified else None,
            "memory": self.memory.model_dump(),
            "active_case_id": self.active_case_id,
            "intent_path": self.intent_path,
            "verify_attempts": self.verify_attempts,
            "off_topic_streak": self.off_topic_streak,
            "frustration_streak": self.frustration_streak,
            "last_emotion": self.last_emotion,
            "outbox": [entry.model_dump() for entry in self.outbox],
            "handoff_note": self.handoff_note,
        }
        if self.caller_role == "representative":
            rep = self.representative
            public.update(
                {
                    "caller_role": self.caller_role,
                    "representative_name": rep.name if rep and rep.name else None,
                    "representative_relationship": rep.relationship if rep else None,
                    "consent_status": self.consent_status,
                }
            )
        return public
