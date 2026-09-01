"""
Turning a verdict into something AEGIS can say.

Two constraints shape everything here.

**Agora caps ``speak`` text at 512 bytes.** So the budget is spent
deliberately: lead with the highest-tier finding, add further reasons only
while they fit whole, and end with the question that puts the decision back
with the humans. Truncating mid-reason would cut an intervention off at
"rolling back Core will break payment-api and au" -- worse than not saying
the second reason at all.

**Nothing uncertain may be spoken as settled.** Quality Standard §4 red line
#4 makes it a hard fail to voice a hypothesis in fact-asserting language.
So hypotheses are always attributed or hedged, evidence is always
attributed to its source, and low-certainty readings always carry their
doubt into the sentence. This is enforced by construction: the phrasing is
built from typed findings, never by interpolating raw claim text into an
assertive template.
"""

from __future__ import annotations

from typing import Optional, Sequence

from backend.common.enums import GovernorAction, RiskFindingCode, RiskTier
from backend.common.errors import SpeechTooLongError
from backend.common.models import RiskFinding, RiskVerdict

#: Agora's documented limit for the ``speak`` endpoint's ``text`` field.
SPEAK_MAX_BYTES = 512

#: Openers per action. WARN leads with a stop word because it is spoken over
#: a live conversation and has to claim attention in its first syllable.
_OPENERS: dict[GovernorAction, str] = {
    GovernorAction.WARN: "Hold —",
    GovernorAction.ASK: "Quick check —",
    GovernorAction.SUGGEST: "Worth noting —",
}

#: Closers that hand the decision back. AEGIS never decides; the sentence it
#: ends on should make that obvious to anyone listening.
#:
#: Two sets, because the two situations are genuinely different. When an
#: action is on the table the right question is whether to proceed. When a
#: bare claim has been contradicted there is nothing to proceed *with* --
#: asking "go ahead anyway?" would invent an action nobody proposed.
_ACTION_CLOSERS: dict[GovernorAction, str] = {
    GovernorAction.WARN: "Do you want to go ahead anyway?",
    GovernorAction.ASK: "Want to confirm before this goes ahead?",
    GovernorAction.SUGGEST: "",
}

_CLAIM_CLOSERS: dict[GovernorAction, str] = {
    GovernorAction.WARN: "Want to re-check before ruling it out?",
    GovernorAction.ASK: "Worth checking that directly?",
    GovernorAction.SUGGEST: "",
}

#: Findings that can only arise from evaluating a proposed action. If a
#: verdict contains none of them, it is grounding a spoken claim instead.
_ACTION_ONLY_CODES = frozenset(
    {
        RiskFindingCode.STALE_JUSTIFICATION,
        RiskFindingCode.DECISION_REVERSAL,
        RiskFindingCode.BLAST_RADIUS_SCHEMA_BREAK,
    }
)


def _closer_for(verdict: RiskVerdict, action: GovernorAction) -> str:
    concerns_an_action = any(code in _ACTION_ONLY_CODES for code in verdict.codes)
    table = _ACTION_CLOSERS if concerns_an_action else _CLAIM_CLOSERS
    return table[action]


def build_intervention_text(
    verdict: RiskVerdict,
    action: GovernorAction,
    *,
    max_bytes: int = SPEAK_MAX_BYTES,
) -> str:
    """Compose the spoken intervention for a verdict.

    Raises :class:`SpeechTooLongError` only if even a single reason cannot
    fit, which would mean a finding message was authored far outside the
    length the format allows -- a bug worth failing loudly on rather than
    papering over with a truncated sentence.
    """
    if action is GovernorAction.SILENT:
        return ""
    if not verdict.findings:
        raise SpeechTooLongError("cannot voice a verdict with no findings", action=action.value)

    opener = _OPENERS[action]
    closer = _closer_for(verdict, action)

    findings = list(verdict.findings)
    lead = findings[0]
    rest = findings[1:]

    # Fit the reasons first, then announce how many there actually are. The
    # announcement has to match what gets said: promising "two issues" and
    # then listing three (or one) is the kind of detail that makes a system
    # sound unreliable in the exact moment it needs to be believed.
    messages = [lead.message]
    single = _compose(opener, messages, closer)
    if _byte_length(single) > max_bytes:
        single = _compose(opener, messages, "")
        if _byte_length(single) > max_bytes:
            raise SpeechTooLongError(
                "a single finding does not fit in the speak budget",
                bytes=_byte_length(single),
                max_bytes=max_bytes,
            )

    candidate = single
    for finding in rest:
        attempt_messages = messages + [finding.message]
        attempt = _compose(opener, attempt_messages, closer)
        if _byte_length(attempt) > max_bytes:
            break
        messages = attempt_messages
        candidate = attempt

    return candidate


#: Spoken counts. Beyond three, "a few" is both shorter and more natural than
#: enumerating -- and the budget will usually have cut the list by then anyway.
_COUNT_WORDS = {2: "two", 3: "three", 4: "four", 5: "five"}


def _compose(opener: str, messages: Sequence[str], closer: str) -> str:
    if len(messages) == 1:
        # A single reason follows the em-dash as a continuation, so it is not
        # capitalised: "Hold — telemetry shows...", not "Hold — Telemetry".
        return _assemble(opener, [_sentence(messages[0], capitalise=False)], closer)
    count = _COUNT_WORDS.get(len(messages), "a few")
    prefix = f"{opener} {count} issues."
    bodies = [_sentence(message) for message in messages]
    return _assemble(prefix, bodies, closer)


def build_status_summary(
    *,
    open_hypotheses: Sequence[str],
    held_decisions: Sequence[str],
    unresolved_actions: Sequence[str],
    max_bytes: int = SPEAK_MAX_BYTES,
) -> str:
    """The spoken answer to "AEGIS, status?".

    Every hypothesis is voiced as a hypothesis. The phrasing ("still open",
    "not confirmed") is not decoration -- speaking an unconfirmed theory in
    settled language is a hard fail.
    """
    parts: list[str] = []

    if open_hypotheses:
        parts.append(
            f"{_count(len(open_hypotheses), 'theory', 'theories')} still open and unconfirmed: "
            + _join(open_hypotheses)
        )
    else:
        parts.append("No open theories")

    if held_decisions:
        parts.append(f"decisions on record: {_join(held_decisions)}")

    if unresolved_actions:
        parts.append(
            f"{_count(len(unresolved_actions), 'action', 'actions')} still awaiting a decision: "
            + _join(unresolved_actions)
        )
    else:
        parts.append("nothing awaiting a decision")

    summary = "Status. " + "; ".join(parts) + "."
    return _fit(summary, max_bytes)


def describe_no_intervention(verdict: RiskVerdict) -> str:
    """Human-readable explanation of a SILENT decision, for the log and UI."""
    if verdict.risk_tier is RiskTier.LOW:
        return "no risk findings"
    return f"{verdict.risk_tier.value} verdict suppressed"


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _assemble(opener: str, bodies: Sequence[str], closer: str) -> str:
    chunks = [opener.strip()] + [body for body in bodies] + ([closer] if closer else [])
    return " ".join(chunk for chunk in chunks if chunk).strip()


def _sentence(message: str, *, capitalise: bool = True) -> str:
    cleaned = " ".join(message.split()).strip()
    if not cleaned:
        return ""
    if capitalise:
        cleaned = cleaned[0].upper() + cleaned[1:]
    if cleaned[-1] not in ".?!":
        cleaned += "."
    return cleaned


def _byte_length(text: str) -> int:
    return len(text.encode("utf-8"))


def _fit(text: str, max_bytes: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    # Cut on a whole word, then on a whole character, so the result is always
    # valid UTF-8 and never ends mid-word.
    clipped = encoded[: max_bytes - 1].decode("utf-8", errors="ignore")
    if " " in clipped:
        clipped = clipped[: clipped.rfind(" ")]
    return clipped.rstrip(" ,;:—-") + "…"


def _join(values: Sequence[str]) -> str:
    # Items are joined into a larger sentence, so their own terminal
    # punctuation has to go -- otherwise the summary reads "...the pool
    # then.; nothing awaiting a decision."
    cleaned = [" ".join(value.split()).rstrip(" .;,") for value in values if value.strip()]
    if not cleaned:
        return "none"
    if len(cleaned) == 1:
        return cleaned[0]
    if len(cleaned) == 2:
        return f"{cleaned[0]} and {cleaned[1]}"
    return f"{', '.join(cleaned[:-1])}, and {cleaned[-1]}"


def _count(number: int, singular: str, plural: str) -> str:
    word = singular if number == 1 else plural
    return f"{number} {word}"
