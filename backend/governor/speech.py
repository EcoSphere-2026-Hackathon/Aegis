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

#: Findings whose message is already a question. Appending "do you want to go
#: ahead anyway?" to "which one did you mean?" would ask two different
#: questions in one breath and invite an answer to whichever was heard last --
#: which, for a confirmation, is the failure this finding exists to prevent.
_SELF_CLOSING_CODES = frozenset({RiskFindingCode.AMBIGUOUS_CONFIRMATION})


def _closer_for(verdict: RiskVerdict, action: GovernorAction) -> str:
    if any(code in _SELF_CLOSING_CODES for code in verdict.codes):
        return ""
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

    # The most severe finding is mandatory. It sets the tier of the whole
    # intervention, so dropping it in favour of a cheaper set would make
    # AEGIS say "quick check" about something that warranted "hold".
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

    # Everything else competes for what is left of the 512 bytes. Choosing
    # greedily by severity is the obvious approach and it is wrong: one
    # verbose HIGH can crowd out two short findings that together say more.
    # This is a 0/1 knapsack — items with a value and a byte cost, one fixed
    # capacity — so it is solved as one, exactly, by dynamic programming.
    # n is the number of findings on a single action, so the table is tiny
    # and an exact solve costs less than the string building around it.
    if rest:
        # The budget has to be measured against the *widest* frame the final
        # sentence could take. The count word grows with the number of
        # findings ("two" -> "three"), so sizing the headroom against the
        # one-finding frame would let an optimal pack land two bytes over the
        # cap — which, at 512, is the difference between speaking and raising
        # SpeechTooLongError in front of the room.
        frame = max(
            _byte_length(_compose(opener, messages, closer, count=count))
            for count in range(2, len(findings) + 1)
        )
        chosen = _select_within_budget(rest, max_bytes - frame)
        messages.extend(finding.message for finding in chosen)

    spoken = _compose(opener, messages, closer)
    # The packing is exact, so this cannot trip; it is asserted anyway because
    # a silent overrun here is a rejected ``speak`` call at the worst moment.
    if _byte_length(spoken) > max_bytes:  # pragma: no cover - guarded by construction
        raise SpeechTooLongError(
            "composed intervention exceeded the speak budget",
            bytes=_byte_length(spoken),
            max_bytes=max_bytes,
        )
    return spoken


#: What one finding is worth saying. HIGH dominates MEDIUM by more than two
#: to one, so the solver never trades a severe finding for a pair of mild
#: ones; the flat bonus breaks ties toward saying *more*, since two reasons
#: at equal value carry more information than one.
_TIER_VALUE: dict[RiskTier, int] = {RiskTier.HIGH: 100, RiskTier.MEDIUM: 40}
_ITEM_BONUS = 1


def _select_within_budget(findings: Sequence[RiskFinding], capacity_bytes: int) -> list[RiskFinding]:
    """Exact 0/1 knapsack over the remaining speech budget.

    Weight is what the finding actually costs once rendered — the sentence
    plus the space that joins it — not the raw message length, because
    budgeting against a number that differs from what gets transmitted is how
    an intervention ends up one byte over the limit at the worst moment.
    """
    if capacity_bytes <= 0:
        return []

    items: list[tuple[RiskFinding, int, int]] = []
    for finding in findings:
        cost = _byte_length(" " + _sentence(finding.message))
        if cost <= capacity_bytes:
            items.append((finding, cost, _TIER_VALUE.get(finding.tier, 10) + _ITEM_BONUS))
    if not items:
        return []

    # best[c] = maximum value achievable using exactly the items considered
    # so far within capacity c. Standard one-dimensional formulation,
    # iterating capacity downward so each item is used at most once.
    best = [0] * (capacity_bytes + 1)
    keep: list[list[bool]] = []

    for _finding, cost, value in items:
        taken = [False] * (capacity_bytes + 1)
        for capacity in range(capacity_bytes, cost - 1, -1):
            candidate = best[capacity - cost] + value
            if candidate > best[capacity]:
                best[capacity] = candidate
                taken[capacity] = True
        keep.append(taken)

    # Walk the decision table back to recover which items the optimum used.
    selected: list[RiskFinding] = []
    capacity = capacity_bytes
    for index in range(len(items) - 1, -1, -1):
        if keep[index][capacity]:
            finding, cost, _value = items[index]
            selected.append(finding)
            capacity -= cost

    selected.reverse()
    # Severity order for delivery: the set is chosen by value, but it is read
    # out worst-first, because that is the order a listener needs it in.
    selected.sort(key=lambda finding: (-finding.tier.rank, finding.code.value))
    return selected


#: Spoken counts. Beyond three, "a few" is both shorter and more natural than
#: enumerating -- and the budget will usually have cut the list by then anyway.
_COUNT_WORDS = {2: "two", 3: "three", 4: "four", 5: "five"}


def _compose(
    opener: str,
    messages: Sequence[str],
    closer: str,
    *,
    count: Optional[int] = None,
) -> str:
    """Assemble the sentence.

    ``count`` overrides the spoken count word, which is what lets the budget
    be measured against a frame wider than the messages currently in hand.
    """
    if len(messages) == 1 and count is None:
        # A single reason follows the em-dash as a continuation, so it is not
        # capitalised: "Hold — telemetry shows...", not "Hold — Telemetry".
        return _assemble(opener, [_sentence(messages[0], capitalise=False)], closer)
    spoken_count = max(count if count is not None else len(messages), 2)
    count_word = _COUNT_WORDS.get(spoken_count, "a few")
    prefix = f"{opener} {count_word} issues."
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
    chunks = [opener.strip(), *bodies, *([closer] if closer else [])]
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
