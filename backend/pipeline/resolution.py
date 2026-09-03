"""
Which pending action does this human reply answer?

This is the single most dangerous question in the system. Everything else
AEGIS does is advisory: it observes, it reasons, it says something. This is
the one place where a human utterance changes the authorisation state of a
consequential action, and getting it wrong means a person's "yes" lands on an
action they were not talking about.

The previous implementation answered it with a heuristic -- named target if
there is one, otherwise the most recent pending action -- and logged a
warning when it was guessing. A logged warning is not a safety property. In a
room with two open actions, "yeah, go ahead" would silently authorise
whichever was proposed last, which is not what the words mean and not what
anybody said.

So the policy here is explicit, total, and refuses rather than guesses:

* **A named target decides it.** If the reply names a component, only actions
  on that component are candidates, narrowed further by action kind when the
  reply carries one. A person who says "yes, roll core-db back" has removed
  the ambiguity themselves, and the action's age no longer matters -- they
  named it, so they meant it.
* **A bare reply requires exactly one open action.** "Go ahead" is only
  unambiguous when there is one thing it could be about. With two open, the
  reply is not resolvable, full stop.
* **A bare reply also has to be timely.** An action stays pending forever,
  which is the right answer to "nobody replied"; but a "yeah" twenty minutes
  into a different conversation is not a decision about it. The clock runs
  from the last moment the action was *live in the room* -- when it was
  proposed, or the last time AEGIS raised it -- so a confirmation after a
  long discussion that AEGIS re-opened still counts.
* **Ambiguity is a finding, not an error.** When the policy cannot resolve,
  the caller asks which one was meant. Nothing is authorised in the meantime,
  and that is the correct resting state.

The module is pure -- it reads no store and mutates nothing -- for the same
reason ``risk_engine`` is: a decision procedure this consequential should be
exhaustively testable without a database, and impossible to accidentally give
a side effect.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Mapping, Optional, Sequence

from backend.common.models import ExtractedClaim, ProposedAction


class ResolutionOutcome(str, Enum):
    """Why a reply did or did not resolve an action."""

    RESOLVED = "resolved"
    """Exactly one action answers to this reply."""

    NOTHING_PENDING = "nothing_pending"
    """A decision was heard with no open action to apply it to."""

    NO_SUCH_TARGET = "no_such_target"
    """The reply named a component that has nothing open on it."""

    AMBIGUOUS = "ambiguous"
    """Several open actions could be meant, and nothing distinguishes them."""

    OUT_OF_WINDOW = "out_of_window"
    """One open action, but it went quiet long enough ago that a bare reply
    is more likely about something else."""

    @property
    def is_resolution(self) -> bool:
        return self is ResolutionOutcome.RESOLVED

    @property
    def needs_clarification(self) -> bool:
        """Whether a human should be asked which action they meant.

        Only the two cases where a decision was clearly *intended* and could
        not be placed. "Nothing pending" needs no question -- there is
        nothing to ask about.
        """
        return self in (ResolutionOutcome.AMBIGUOUS, ResolutionOutcome.OUT_OF_WINDOW)


@dataclass(frozen=True)
class ResolutionDecision:
    """The outcome, the action if there is one, and what it was chosen from.

    ``candidates`` is carried even when nothing resolved, because that is
    exactly what the clarifying question needs to name.
    """

    outcome: ResolutionOutcome
    action: Optional[ProposedAction] = None
    candidates: tuple[ProposedAction, ...] = ()
    reason: str = ""

    def __post_init__(self) -> None:
        # Structural, not documentary: an outcome that is not RESOLVED must
        # not carry an action, or a caller reading ``.action`` without
        # checking the outcome would authorise something the policy refused.
        if self.outcome.is_resolution and self.action is None:
            raise ValueError("a resolved decision must carry the action it resolved")
        if not self.outcome.is_resolution and self.action is not None:
            raise ValueError(f"outcome {self.outcome.value} must not carry an action")

    @property
    def candidate_ids(self) -> tuple[str, ...]:
        return tuple(action.claim_id for action in self.candidates)


def select_action_to_resolve(
    pending: Sequence[ProposedAction],
    claim: ExtractedClaim,
    *,
    window_seconds: float,
    last_raised_at: Optional[Mapping[str, datetime]] = None,
    utterance: Optional[str] = None,
) -> ResolutionDecision:
    """Apply the policy. Never raises; never guesses.

    ``last_raised_at`` maps an action's claim id to the last time AEGIS spoke
    about it. Absent entries simply mean AEGIS never raised that action, in
    which case its proposal time is the reference moment.

    ``utterance`` is the human's actual words. It is what ``target_ref`` is
    checked against before the named-target path is allowed to run; see
    :func:`_target_is_corroborated`. Defaults to the claim's own text, which
    is the same string for every extractor in this repository.
    """
    if not pending:
        return ResolutionDecision(
            outcome=ResolutionOutcome.NOTHING_PENDING,
            reason="no action is awaiting a decision",
        )

    raised = last_raised_at or {}

    # A target the human did not actually say is not a disambiguation, it is
    # the extractor's guess -- and acting on it would let interpretation pick
    # which action gets authorised. Fall through to the bare-reply rules,
    # which require exactly one open action *and* timeliness, and refuse
    # otherwise. See the module docstring's second rule.
    if claim.target_ref and _target_is_corroborated(
        claim.target_ref, utterance if utterance is not None else claim.text
    ):
        return _resolve_named(pending, claim)

    if len(pending) > 1:
        return ResolutionDecision(
            outcome=ResolutionOutcome.AMBIGUOUS,
            candidates=tuple(pending),
            reason=(
                f"the reply names no target and {len(pending)} actions are open, "
                f"so which one it answers cannot be established"
            ),
        )

    only = pending[0]
    reference = _reference_moment(only, raised)
    age = (claim.timestamp - reference).total_seconds()
    if age > window_seconds:
        return ResolutionDecision(
            outcome=ResolutionOutcome.OUT_OF_WINDOW,
            candidates=(only,),
            reason=(
                f"the only open action last came up {int(age)}s ago, beyond the "
                f"{int(window_seconds)}s window in which a bare reply is about it"
            ),
        )

    return ResolutionDecision(
        outcome=ResolutionOutcome.RESOLVED,
        action=only,
        candidates=(only,),
        reason="one action is open and it is still live in the conversation",
    )


def _resolve_named(
    pending: Sequence[ProposedAction], claim: ExtractedClaim
) -> ResolutionDecision:
    """The reply named a component.

    Age is deliberately not checked on this path. Naming the target *is* the
    disambiguation: "yes, roll core-db back" means that rollback whether it
    was proposed thirty seconds or thirty minutes ago, and refusing it on
    age would be refusing an unambiguous human instruction.
    """
    on_target = tuple(
        action for action in pending if action.target_ref == claim.target_ref
    )
    if not on_target:
        return ResolutionDecision(
            outcome=ResolutionOutcome.NO_SUCH_TARGET,
            reason=f"nothing is open on {claim.target_ref}",
        )

    candidates = on_target
    if len(candidates) > 1 and claim.action_kind is not None:
        # Two things can be open on one component -- "roll core-db back" and
        # "restart core-db" -- and a reply that names the kind has picked
        # one of them.
        narrowed = tuple(
            action for action in candidates if action.action_kind is claim.action_kind
        )
        if narrowed:
            candidates = narrowed

    if len(candidates) == 1:
        return ResolutionDecision(
            outcome=ResolutionOutcome.RESOLVED,
            action=candidates[0],
            candidates=candidates,
            reason=f"the reply names {claim.target_ref} and one action is open on it",
        )

    return ResolutionDecision(
        outcome=ResolutionOutcome.AMBIGUOUS,
        candidates=candidates,
        reason=(
            f"{len(candidates)} actions are open on {claim.target_ref} and the reply "
            f"does not say which"
        ),
    )


#: Tokens too short or too generic to be evidence that a human named a
#: component. "db" and "api" appear inside half the topology; requiring a
#: distinctive token keeps the check from passing on any sentence containing
#: the word "service".
_MIN_TOKEN = 3

#: Splits a component name into the words a person might actually say.
#: ``core-db`` -> {"core", "db"}; ``notification-service`` -> {"notification",
#: "service"}.
_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")


def _target_is_corroborated(target_ref: str, utterance: str) -> bool:
    """Did the human's own words refer to this component?

    The single most dangerous thing an extractor can do to this system is
    attach a ``target_ref`` to a bare "yeah". The vocabulary constraint in the
    extraction service stops invented component names, but it cannot stop a
    *real* one being attached to an utterance that never mentioned it -- and
    the model is handed the list of pending targets as context, which is
    exactly the set that would cause an authorisation if guessed.

    Without this check the named-target path fires on that guess and
    authorises a specific action, skipping both the "exactly one open action"
    rule and the timeliness rule. That is interpretation becoming
    authorisation, which is the one boundary this system exists to hold.

    So the reply only counts as *naming* a target when the utterance carries
    lexical evidence of it: the full name (however it is punctuated), or one
    of its distinctive words. This is deliberately generous -- it is a guard
    against fabrication, not a disambiguator -- because the cost of being
    wrong is asymmetric. A false negative falls through to the bare-reply
    rules, which still resolve a single timely action. A false positive
    authorises the wrong destructive operation.
    """
    if not target_ref:
        return False

    spoken = utterance.lower()
    # Punctuation-insensitive: "core-db", "core db" and "coredb" are the same
    # component said three ways, and none of them should be a miss.
    flattened = _TOKEN_SPLIT.sub("", spoken)
    target = target_ref.lower()

    if target in spoken or _TOKEN_SPLIT.sub("", target) in flattened:
        return True

    words = {word for word in _TOKEN_SPLIT.split(spoken) if word}
    return any(
        token in words
        for token in _TOKEN_SPLIT.split(target)
        if len(token) >= _MIN_TOKEN
    )


def _reference_moment(
    action: ProposedAction, raised: Mapping[str, datetime]
) -> datetime:
    """When this action was last live in the room.

    The later of "it was proposed" and "AEGIS last raised it". Using only the
    proposal time would time out a confirmation that follows a discussion
    AEGIS itself re-opened, which is the most natural moment for a human to
    finally answer.
    """
    last_raised = raised.get(action.claim_id)
    if last_raised is None:
        return action.timestamp
    return max(action.timestamp, last_raised)


def describe_candidates(candidates: Sequence[ProposedAction], *, limit: int = 3) -> str:
    """Name the open actions in the words a person would use.

    Kept here rather than in the speech layer because it describes ledger
    entries, and the speech layer's job is composing sentences from typed
    findings, not reaching back into state.
    """
    described = [
        f"the {action.target_ref} {action.action_kind.value}" for action in candidates[:limit]
    ]
    if not described:
        return ""
    if len(described) == 1:
        return described[0]
    if len(candidates) > limit:
        return ", ".join(described) + ", or something else still open"
    return ", ".join(described[:-1]) + f", or {described[-1]}"


def clarification_message(decision: ResolutionDecision) -> str:
    """The question AEGIS asks when it will not guess.

    Phrased so the sentence is true about the ledger and asserts nothing
    about the world -- and so it ends in a question, because handing the
    decision back is the entire point of asking.
    """
    named = describe_candidates(decision.candidates)
    if decision.outcome is ResolutionOutcome.OUT_OF_WINDOW:
        return (
            f"the only action still open is {named}, from a while back. "
            f"Did you mean that one?"
        )
    count = _SPELLED.get(len(decision.candidates), "several")
    return (
        f"{count} actions are still open: {named}. Which one did you mean?"
    )


#: Spoken out, not printed. AEGIS's output is read aloud by a voice engine
#: over a live call, and a numeral in the middle of a sentence is a small bet
#: on how that engine pronounces it.
_SPELLED = {2: "two", 3: "three", 4: "four", 5: "five"}


#: Exported for the orchestrator's window arithmetic, so the two agree on
#: what "recently" means without importing config into a pure module.
DEFAULT_WINDOW = timedelta(seconds=120).total_seconds()
