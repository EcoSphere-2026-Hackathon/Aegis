"""
Offline, rule-based extraction provider.

Two jobs, both real:

* **Test double.** Every pipeline test runs the exact code path a live
  provider would -- same request, same JSON response envelope, same
  validation and normalisation -- with no network and no key.
* **Demo fallback.** If the LLM key is missing or the vendor is down during
  judging, AEGIS still extracts, still reasons, still intervenes. A demo that
  dies because an API call timed out is a worse outcome than a demo that
  degrades to rules and says so.

What it is **not** is the product's claim. The pitch is LLM extraction; this
is the safety net underneath it, and the structured logs record which
provider produced every claim so the two can never be confused after the
fact.

Note what this file does *not* do: it makes no risk judgements. Rule-based
*understanding* is a legitimate fallback for the understanding stage. The
boundary that matters -- that risk decisions are deterministic and separate
from interpretation -- is untouched, because that decision still happens in
``risk_engine`` regardless of which provider produced the claim.
"""

from __future__ import annotations

import json
import re
import time
from typing import Iterable, Optional, Sequence

from backend.extraction.contracts import (
    ExtractionContext,
    ProviderRequest,
    ProviderResponse,
)

# --- cue vocabularies ------------------------------------------------------
# Ordered by specificity: the first matching category wins, so "hold off on
# the rollback" is a hold rather than a rollback proposal.

_HEDGE_CUES = (
    "might be", "maybe", "probably", "looks like", "look like", "i think",
    "seems", "seem to", "could be", "not sure", "my guess", "i'd guess",
    "suspect", "possibly", "presumably", "appears", "feels like", "pretty sure",
)
_HOLD_CUES = (
    "hold off", "hold on", "let's hold", "hold", "wait", "pause", "not yet",
    "before we do", "stop", "hang on", "hang fire",
)
_OVERRIDE_CUES = (
    "don't", "do not", "no,", "nope", "cancel", "abort", "never mind", "scrap that",
)

#: Cues matched on word boundaries rather than as substrings, because the
#: bare words are short enough to appear inside unrelated ones ("hold" in
#: "household", "stop" in "stopgap", "no," is fine but "no" alone is not).
_WORD_BOUNDARY_CUES = frozenset({"hold", "wait", "pause", "stop", "no", "nope"})
_CONFIRMATION_CUES = (
    "go ahead", "do it", "yes,", "yep", "yeah, do", "approved", "confirmed",
    "ship it", "green light", "i approve", "sounds good, do",
)
_PROPOSAL_CUES = (
    "let's", "lets ", "we should", "i'll", "i will", "we can", "can we",
    "shall we", "going to", "gonna", "propose", "how about we",
)
#: Bare assertions of cause. "It's the pool" is not an observation -- it is an
#: unverified root-cause claim stated as settled, which is precisely the
#: failure mode this product exists to catch. Classifying it as a fact would
#: let the very thing AEGIS is meant to notice pass unnoticed.
_CAUSAL_CUES = (
    "it is the", "it's the", "that's the", "thats the", "the cause is",
    "root cause", "caused by", "because of", "due to", "culprit",
    "it is ", "it's ",
)
_DECISION_CUES = (
    "we're going with", "we are going with", "decision is", "we've decided",
    "we have decided", "we're not", "we are not", "we'll hold", "we will hold",
    "agreed", "final answer", "let's go with", "we're holding", "we are holding",
)

_ACTION_KIND_CUES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("rollback", ("roll back", "rollback", "revert", "roll it back", "previous version", "last version")),
    ("migration", ("migrate", "migration", "schema change", "apply the migration")),
    ("failover", ("fail over", "failover", "switch to the replica", "promote the replica")),
    ("restart", ("restart", "reboot", "bounce", "cycle the", "recycle")),
    ("scale", ("scale up", "scale down", "scale out", "add replicas", "bump the pool", "resize")),
    ("config_change", ("config", "flag", "toggle", "set the", "change the setting", "increase the limit")),
)

_NUMBER_PATTERN = re.compile(r"(?<![\w.])(\d{1,3}(?:\.\d+)?)\s*(%|percent|ms|milliseconds|s\b|seconds)?")
_VERSION_PATTERN = re.compile(r"\bv(\d+(?:\.\d+)*)\b", re.IGNORECASE)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\s*[;]\s+")


class DeterministicProvider:
    """Rule-based extraction over the structured request context."""

    name = "deterministic"

    def supports_vision(self) -> bool:
        return False

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        started = time.perf_counter()
        claims = self._extract(request.utterance, request.context)
        payload = {"claims": claims or [{"type": "none", "text": ""}]}
        return ProviderResponse(
            raw_text=json.dumps(payload),
            model="rules-v1",
            provider=self.name,
            latency_ms=(time.perf_counter() - started) * 1000.0,
        )

    # -- extraction -------------------------------------------------------

    def _extract(self, utterance: str, context: ExtractionContext) -> list[dict]:
        claims: list[dict] = []
        for sentence in self._sentences(utterance):
            claims.extend(self._classify(sentence, context))
        substantive = [claim for claim in claims if claim.get("type") != "none"]
        return substantive or claims

    @staticmethod
    def _sentences(utterance: str) -> list[str]:
        """One claim per assertion, so a single turn can produce several.

        "Okay, it's the pool then. Let's roll Core back." is two claims: a
        hypothesis and a proposed action. Treating it as one would lose
        whichever half the classifier did not pick.
        """
        parts = [part.strip() for part in _SENTENCE_SPLIT.split(utterance.strip()) if part.strip()]
        return parts or ([utterance.strip()] if utterance.strip() else [])

    def _classify(self, sentence: str, context: ExtractionContext) -> list[dict]:
        lowered = sentence.lower()
        target = self._match_target(lowered, context.known_targets)
        metric, value, unit = self._match_metric(
            lowered, context.known_metrics, context.metric_aliases
        )
        pending = set(context.pending_action_targets)

        base: dict = {"text": sentence.strip()}
        if target:
            base["target_ref"] = target
        if metric:
            base["metric_ref"] = metric
            if value is not None:
                base["claimed_value"] = value
                base["claimed_unit"] = unit

        # Resolutions come first: while something awaits a decision, a refusal
        # or a hold is about that, even when the same breath proposes what to
        # do instead. "Hold — don't roll back, let's check the metrics first"
        # is a hold, and reading it as a fresh rollback proposal would be
        # exactly backwards.
        if pending:
            resolution = self._resolution_type(lowered)
            if resolution is not None:
                claims = [{**base, "type": resolution}]
                # If the same sentence also proposes something on a *different*
                # component, that proposal is real and must not be swallowed --
                # a missed proposed action is the most dangerous thing this
                # extractor can drop.
                if target and target not in pending and self._action_kind(lowered):
                    claims.append(self._proposal_claim(base, sentence, lowered, target))
                return claims

        if self._contains(lowered, _DECISION_CUES) or (
            self._contains(lowered, _HOLD_CUES) and target is not None
        ):
            stance = "hold" if self._is_negative(lowered) else "proceed"
            return [{**base, "type": "decision", "decision_stance": stance}]

        if self._contains(lowered, _PROPOSAL_CUES) or self._action_kind(lowered) is not None:
            if target is None:
                # An action nobody can locate is not risk-evaluable; report
                # what was heard as a hypothesis rather than inventing a
                # target the topology would silently fail to match.
                return [{**base, "type": "hypothesis"}]
            return [self._proposal_claim(base, sentence, lowered, target)]

        if self._contains(lowered, _HEDGE_CUES):
            return [{**base, "type": "hypothesis"}]

        # An unhedged causal claim about a component or metric is still a
        # theory, however confidently it was said.
        if (target or metric) and self._contains(lowered, _CAUSAL_CUES):
            return [{**base, "type": "hypothesis"}]

        # A bare metric reading someone recites from impression is a hedge,
        # not an observation: "pool utilization is about 40" is exactly the
        # claim the product exists to check against reality.
        if metric and value is not None:
            return [{**base, "type": "hypothesis"}]

        if self._is_substantive(lowered):
            return [{**base, "type": "fact"}]

        return [{"type": "none", "text": ""}]

    def _resolution_type(self, lowered: str) -> Optional[str]:
        """Hold beats decline beats approve.

        Ordered by how conservative the outcome is: mishearing an approval as
        a hold leaves an action pending, which is safe. Mishearing a hold as
        an approval authorises something nobody agreed to, which is the one
        failure this system exists to prevent.
        """
        if self._contains(lowered, _HOLD_CUES):
            return "hold"
        if self._contains(lowered, _OVERRIDE_CUES):
            return "override"
        if self._contains(lowered, _CONFIRMATION_CUES):
            return "confirmation"
        return None

    def _proposal_claim(self, base: dict, sentence: str, lowered: str, target: str) -> dict:
        claim = {**base, "type": "proposed_action", "target_ref": target,
                 "action_kind": self._action_kind(lowered) or "other"}
        version = _VERSION_PATTERN.search(sentence)
        if version:
            claim["target_schema_version"] = f"v{version.group(1)}"
        return claim

    # -- matching helpers -------------------------------------------------

    @staticmethod
    def _contains(text: str, cues: Sequence[str]) -> bool:
        for cue in cues:
            if cue in _WORD_BOUNDARY_CUES:
                if re.search(rf"\b{re.escape(cue)}\b", text):
                    return True
            elif cue in text:
                return True
        return False

    @staticmethod
    def _is_negative(text: str) -> bool:
        return any(cue in text for cue in _OVERRIDE_CUES + _HOLD_CUES)

    @staticmethod
    def _action_kind(text: str) -> Optional[str]:
        for kind, cues in _ACTION_KIND_CUES:
            if any(cue in text for cue in cues):
                return kind
        return None

    @staticmethod
    def _match_target(text: str, known_targets: Sequence[str]) -> Optional[str]:
        """Match a component by full name or by the way people actually say it.

        "Core" means ``core-db``; "payments" means ``payment-api``. Aliases
        are derived from the node names rather than hand-listed, so adding a
        service to the topology does not require editing this file.
        """
        for target in known_targets:
            if target in text:
                return target
        for target in known_targets:
            head = target.split("-")[0]
            if len(head) < 3:
                continue
            if re.search(rf"\b{re.escape(head)}s?\b", text):
                return target
        return None

    @staticmethod
    def _match_metric(
        text: str,
        known_metrics: Sequence[str],
        aliases: Optional[dict] = None,
    ) -> tuple[Optional[str], Optional[float], Optional[str]]:
        """Find a metric and, if one is stated nearby, the value claimed for it.

        Aliases come from the telemetry catalogue rather than from splitting
        the metric name, because people say "the pool", not
        "pool_utilization" -- and a claim that never binds to a metric can
        never be grounded against a reading.

        Longest phrase first, so "pool utilisation" is preferred over the
        bare "pool" and the anchor for the number search lands in the right
        place.
        """
        aliases = aliases or {}
        candidates: list[tuple[str, str]] = []
        for metric in known_metrics:
            candidates.append((metric, metric))
            candidates.append((metric, metric.replace("_", " ")))
            for alias in aliases.get(metric, ()):
                candidates.append((metric, alias.lower()))
        candidates.sort(key=lambda pair: len(pair[1]), reverse=True)

        for metric, phrase in candidates:
            anchor = text.find(phrase)
            if anchor == -1:
                continue
            match = _NUMBER_PATTERN.search(text[anchor:])
            if match:
                unit = match.group(2)
                normalised_unit = "%" if unit in {"%", "percent"} else unit
                return metric, float(match.group(1)), normalised_unit
            return metric, None, None
        return None, None, None

    @staticmethod
    def _is_substantive(text: str) -> bool:
        stripped = text.strip(" .,!?")
        if len(stripped.split()) < 3:
            return False
        filler = {
            "ok", "okay", "right", "sure", "thanks", "yeah", "yep", "mhm",
            "got it", "sounds good", "cool", "alright", "hello", "hi",
        }
        return stripped not in filler
