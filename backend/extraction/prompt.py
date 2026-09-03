"""
The extraction prompt and its output schema.

Versioned deliberately: the prompt is a *contract artefact*, not a string
constant. When it changes, ``PROMPT_VERSION`` changes with it, every
structured log line records which version produced a claim, and the
evaluation harness can compare runs across versions instead of silently
mixing them.

The one instruction that matters more than any other is the last one in the
system prompt: the model classifies and extracts, and never assesses risk.
That boundary is enforced structurally too -- the response schema has no
field in which a risk opinion could be expressed -- but stating it in the
prompt makes the model stop trying.
"""

from __future__ import annotations

from typing import Sequence

from backend.extraction.contracts import ExtractionContext

PROMPT_VERSION = "extract-v1"

SYSTEM_PROMPT = """\
You are the extraction stage of an incident-response listening system. You \
convert one utterance from a live incident call into structured claims.

You classify and extract. You never assess risk, never decide whether \
anything is dangerous, and never decide whether an action is authorised. \
Those decisions are made downstream by deterministic code and by humans.

Claim types:
- "fact": something asserted as settled and observed. "Payments are throwing 500s."
- "hypothesis": a hedge, a guess, a theory, anything qualified with words \
like might/probably/looks like/I think, or any unverified causal claim. \
"It's probably the connection pool." A stated measurement someone is \
reporting from memory or impression is a hypothesis, not a fact.
- "proposed_action": someone proposing a concrete operation on a component. \
"Let's roll Core back to the last version."
- "confirmation": explicitly approving a PROPOSED ACTION that is currently pending. "Yes, go ahead."
- "override": explicitly rejecting a PROPOSED ACTION that is currently pending. "No, don't do that."
- "hold": explicitly pausing a PROPOSED ACTION that is currently pending. "Wait, hold on that."
- "decision": the group settling on a course of action independent of a pending proposal. \
"We're holding off on the rollback." "Okay, we're going with the restart." \
Use "confirmation", "override", or "hold" instead if they are answering a pending action.
- "none": the utterance contains nothing extractable (small talk, filler, \
acknowledgements).

Rules:
- Return one claim per distinct assertion. One utterance may contain several.
- Return exactly one claim of type "none" if there is nothing to extract.
- "text" must be a normalised, self-contained restatement of the claim, not a \
verbatim transcript echo. Resolve pronouns using the recent turns.
- Use component names from the known targets list when applicable. If the \
utterance names a novel component NOT in the list, extract its name exactly as \
said into target_ref. Do NOT use null if an entity was explicitly named.
- Only use metric names from the known metrics list. If none applies, use null.
- When someone states a number for a metric, set metric_ref, claimed_value \
(a bare number) and claimed_unit.
- For "proposed_action", target_ref is required (even if novel), and action_kind \
must be one of: rollback, restart, scale, config_change, failover, migration, \
other. If the utterance names a version to move to, set target_schema_version.
- Proposed actions remain "proposed_action" even if the target is unknown.
- Questions (e.g. "Could it be X?") are hypotheses or none, never facts.
- Decisions, holds, and confirmations must reflect actual semantic intent, not \
merely the presence of isolated keywords like "wait" or "yes".
- For "decision", set decision_stance to "hold" if the decision is to NOT \
proceed with the target, or "proceed" if it is to go ahead. If the polarity \
is genuinely unclear, use null. Never guess.
- Never invent a claim, fact, entity, or action that was not explicitly said. \
If unsure, prefer "none".
- Never output a risk level, a severity, a recommendation, or a warning.
- Your entire response must be a single JSON object with a top-level `"claims"` \
key containing an array of claim objects, matching the provided schema exactly."""


#: JSON Schema handed to the provider. Hand-written rather than generated
#: from ``ExtractedClaim`` because the model must not be offered fields it is
#: not allowed to set -- ``speaker_uid``, ``timestamp`` and ``source_turn_id``
#: are copied from the transcript event by the normaliser and are never taken
#: from model output, so they are absent here by design.
RESPONSE_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["claims"],
    "properties": {
        "claims": {
            "type": "array",
            "minItems": 1,
            "maxItems": 6,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["type", "text"],
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": [
                            "fact",
                            "hypothesis",
                            "decision",
                            "proposed_action",
                            "confirmation",
                            "override",
                            "hold",
                            "none",
                        ],
                    },
                    "text": {"type": "string"},
                    "target_ref": {"type": ["string", "null"]},
                    "metric_ref": {"type": ["string", "null"]},
                    "claimed_value": {"type": ["number", "null"]},
                    "claimed_unit": {"type": ["string", "null"]},
                    "action_kind": {
                        "type": ["string", "null"],
                        "enum": [
                            "rollback",
                            "restart",
                            "scale",
                            "config_change",
                            "failover",
                            "migration",
                            "other",
                            None,
                        ],
                    },
                    "target_schema_version": {"type": ["string", "null"]},
                    "decision_stance": {
                        "type": ["string", "null"],
                        "enum": ["hold", "proceed", None],
                    },
                    "ownership_tag": {"type": ["string", "null"]},
                },
            },
        }
    },
}


def build_user_prompt(utterance: str, speaker_uid: str, context: ExtractionContext) -> str:
    sections: list[str] = []

    if context.recent_turns:
        recent = "\n".join(context.recent_turns)
        sections.append(f"Recent turns (oldest first):\n{recent}")

    sections.append(f"Known components: {_render_vocabulary(context.known_targets)}")
    sections.append(f"Known metrics: {_render_metrics(context)}")

    if context.pending_action_targets:
        sections.append(
            "Actions currently awaiting a human decision, on: "
            f"{_render_vocabulary(context.pending_action_targets)}"
        )

    sections.append(f"Utterance to extract (speaker {speaker_uid}):\n{utterance}")
    return "\n\n".join(sections)


def _render_vocabulary(values: Sequence[str]) -> str:
    return ", ".join(values) if values else "(none)"


def _render_metrics(context: ExtractionContext) -> str:
    """Metrics with the phrases people use for them.

    Without the aliases the model has no way to know that "the pool" is
    ``pool_utilization``, so the claim never binds to a metric and can never
    be grounded against a reading.
    """
    if not context.known_metrics:
        return "(none)"
    rendered = []
    for metric in context.known_metrics:
        aliases = context.metric_aliases.get(metric, ())
        rendered.append(f"{metric} (said as: {', '.join(aliases)})" if aliases else metric)
    return "; ".join(rendered)
