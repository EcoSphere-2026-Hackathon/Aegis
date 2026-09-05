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
from typing import Optional, Sequence

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
#: Hold and refusal, matched by pattern.
#:
#: Inflection is the whole point here. "We're holding off on the rollback" is
#: a hold; a substring list containing "hold off" misses it, because the word
#: in the sentence is "holding". Getting that wrong records the decision with
#: the opposite polarity, and the decision-reversal check -- which exists to
#: catch exactly this failure mode -- then never fires.
_HOLD_PATTERN = re.compile(
    r"\bhold(?:s|ing)?\b(?:\s+(?:off|on|fire))?"
    r"|\bwait(?:s|ing)?\b|\bpaus(?:e|es|ed|ing)\b|\bstop(?:s|ped|ping)?\b"
    r"|\bnot\s+yet\b|\bhang\s+on\b|\bbefore\s+we\s+do\b|\bstand\s+down\b"
)

_OVERRIDE_PATTERN = re.compile(
    r"\bdon'?t\b|\bdo\s+not\b|\bnope\b|\bcancel(?:s|led|ling)?\b"
    r"|\babort(?:s|ed|ing)?\b|\bnever\s+mind\b|\bscrap\s+that\b"
    # Bare "no" only when it is a refusal, not "no problem" / "no idea".
    r"|\bno\b(?!\s+(?:problem|idea|worries|rush|change|one))"
)

#: Any marker that a decision is a decision *not* to proceed.
_NEGATIVE_STANCE_PATTERN = re.compile(
    _HOLD_PATTERN.pattern + r"|" + _OVERRIDE_PATTERN.pattern + r"|\bnot\b|\bleaving\s+\S+\s+alone\b"
)
_CONFIRMATION_CUES = (
    "go ahead", "do it", "yes,", "yep", "yeah, do", "approved", "confirmed",
    "ship it", "green light", "i approve", "sounds good, do",
)

#: A whole utterance that is nothing but an affirmative. Only consulted while
#: something is awaiting a decision, where "yeah" on its own is an answer
#: rather than filler.
#:
#: The list stops where certainty stops, and that boundary is the safety rule
#: rather than a matter of taste. "Yes" and "yeah" are answers. "Okay",
#: "sure", "right" and "mm hmm" are acknowledgements -- a person saying "okay"
#: may be agreeing, or may just be signalling that they heard, and a system
#: that cannot tell those apart must not treat either as authorisation.
_BARE_AFFIRMATIVE = re.compile(
    r"^(?:(?:yes|yeah|yeh|yep|yup|absolutely|definitely|agreed)[\s,.!?]*)+$",
    re.IGNORECASE,
)
_PROPOSAL_CUES = (
    "let's", "lets ", "we should", "i'll", "i will", "we can", "can we",
    "shall we", "going to", "gonna", "propose", "how about we",
)
#: Bare assertions of cause. "It's the pool" is not an observation -- it is an
#: unverified root-cause claim stated as settled, which is precisely the
#: failure mode this product exists to catch. Classifying it as a fact would
#: let the very thing AEGIS is meant to notice pass unnoticed.
#:
#: Split in two because the two halves need different evidence.
#:
#: **Explicit** cues name causation outright, so the sentence is a theory
#: whether or not the thing blamed is in the topology. "It's the retry storm,
#: definitely" was being filed as a settled fact purely because "retry storm"
#: is not a known component -- so the most confident unverified claim in the
#: room became the one nothing could contradict. That is the failure mode
#: inverted. (Found by the labelled extraction evaluation, not by reading the
#: code.)
_EXPLICIT_CAUSAL_CUES = (
    "the cause is", "root cause", "caused by", "because of", "due to",
    "culprit", "is the problem", "what's killing", "whats killing",
    "that's why", "thats why", "explains the", "explains why",
)

#: **Weak** cues are only causal in context: "it's the pool" is a theory,
#: "it's fine" is not. They need a bound component or metric before they mean
#: anything, which is what stops every "it's ..." sentence becoming a theory.
_CAUSAL_CUES = (
    "it is the", "it's the", "that's the", "thats the",
    "it is ", "it's ",
)

#: Words people use to mark certainty. They do not make a claim true, and in
#: this system they are close to the opposite: an unverified cause asserted
#: with emphasis is exactly what the product exists to notice. So a weak
#: causal cue plus one of these is a theory even when the thing being blamed
#: is not a component AEGIS knows about.
_CERTAINTY_MARKERS = (
    "definitely", "for sure", "obviously", "clearly", "no question",
    "100%", "certainly", "i'm certain", "im certain", "guaranteed",
)
_DECISION_CUES = (
    "we're going with", "we are going with", "decision is", "we've decided",
    "we have decided", "we're not", "we are not", "we'll hold", "we will hold",
    "agreed", "final answer", "let's go with", "we're holding", "we are holding",
)

#: Action kinds, matched by pattern rather than substring.
#:
#: Patterns because English separates the verb from its particle: people say
#: "roll core-db back", not "roll back core-db", and a substring match for
#: "roll back" silently misses it. Missing the *kind* is not cosmetic -- the
#: schema-compatibility check only runs for actions that change a schema
#: surface, so an unrecognised rollback is a blast radius nobody checks.
_ACTION_KIND_PATTERNS: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    ("rollback", re.compile(
        r"\broll(?:s|ing|ed)?\b(?:\s+\S+){0,3}?\s+back\b"
        r"|\brollback\b|\broll\s+back\b|\brevert(?:s|ing|ed)?\b"
        r"|\b(?:previous|last|prior)\s+version\b|\bback\s+out\b"
    )),
    ("migration", re.compile(r"\bmigrat(?:e|es|ing|ion)\b|\bschema\s+change\b")),
    ("failover", re.compile(r"\bfail\s*over\b|\bpromote\s+the\s+replica\b|\bswitch\s+to\s+the\s+replica\b")),
    ("restart", re.compile(r"\brestart(?:s|ing|ed)?\b|\breboot(?:s|ing|ed)?\b|\bbounc(?:e|es|ing|ed)\b|\brecycl(?:e|es|ing|ed)\b")),
    ("scale", re.compile(r"\bscale\s+(?:up|down|out|in)\b|\badd\s+replicas\b|\bresiz(?:e|es|ing|ed)\b|\bbump\s+the\s+pool\b")),
    ("config_change", re.compile(r"\bconfig(?:uration)?\b|\bfeature\s+flag\b|\btoggl(?:e|es|ing|ed)\b|\bchange\s+the\s+setting\b|\bincrease\s+the\s+limit\b")),
)

_NUMBER_PATTERN = re.compile(r"(?<![\w.])(\d{1,3}(?:\.\d+)?)\s*(%|percent|ms|milliseconds|s\b|seconds)?")
_VERSION_PATTERN = re.compile(r"\bv(\d+(?:\.\d+)*)\b", re.IGNORECASE)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\s*[;]\s+")

#: Speech recognition writes numbers as words. "Pool utilization looks fine,
#: like 40%" is a sentence somebody typed; what a microphone delivers is
#: "like forty percent". A digits-only reader turns that into a claim with a
#: metric and no value -- and a claim with no value can never be grounded, so
#: it can never be contradicted. Every spoken demo hits this.
_UNITS_TO_NINETEEN = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
_TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fourty": 40,  # 'fourty': common ASR spelling
    "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}
_NUMBER_WORDS = {**_UNITS_TO_NINETEEN, **_TENS}

#: Units as they are spoken.
_SPOKEN_UNITS = {
    "%": "%", "percent": "%", "percentage": "%",
    "ms": "ms", "millisecond": "ms", "milliseconds": "ms",
    "second": "s", "seconds": "s",
}

#: Words that may sit between a number and its unit without breaking it:
#: "forty odd percent", "around ninety percent".
_NUMBER_FILLER = frozenset({"odd", "or", "so", "about", "roughly", "around"})

_TOKENS = re.compile(r"%|[a-z]+")


def _spoken_number(text: str) -> "tuple[Optional[float], Optional[str]]":
    """Read a number written as words, with its unit if one was said.

    Tokenised rather than matched with one large regular expression, because
    the alternations in such a pattern happily match inside longer words --
    "nine" is a prefix of "ninety", so "ninety one percent" reads as 1 unless
    every branch carries its own word boundary. Scanning whole tokens makes
    that class of bug impossible rather than merely fixed.

    Returns ``(None, None)`` when there is no spoken quantity, so the caller
    falls through to the digit pattern unchanged.
    """
    tokens = _TOKENS.findall(text.lower().replace("-", " "))
    index = 0
    while index < len(tokens):
        if tokens[index] not in _NUMBER_WORDS and tokens[index] not in ("a", "one"):
            index += 1
            continue

        total = 0
        seen = False

        if (
            tokens[index] in ("a", "one")
            and index + 1 < len(tokens)
            and tokens[index + 1] == "hundred"
        ):
            total, seen, index = 100, True, index + 2
            if index < len(tokens) and tokens[index] == "and":
                index += 1
        elif tokens[index] == "a":
            index += 1
            continue

        if index < len(tokens) and tokens[index] in _TENS:
            total += _TENS[tokens[index]]
            seen = True
            index += 1
            # "forty five" is 45. Only a tens word may take a trailing unit.
            if index < len(tokens) and tokens[index] in _UNITS_TO_NINETEEN:
                total += _UNITS_TO_NINETEEN[tokens[index]]
                index += 1
        elif index < len(tokens) and tokens[index] in _UNITS_TO_NINETEEN:
            total += _UNITS_TO_NINETEEN[tokens[index]]
            seen = True
            index += 1

        if not seen:
            index += 1
            continue

        value = float(total)
        if (
            index + 1 < len(tokens)
            and tokens[index] == "point"
            and tokens[index + 1] in _UNITS_TO_NINETEEN
        ):
            value += _UNITS_TO_NINETEEN[tokens[index + 1]] / 10.0
            index += 2

        while index < len(tokens) and tokens[index] in _NUMBER_FILLER:
            index += 1
        unit = _SPOKEN_UNITS.get(tokens[index]) if index < len(tokens) else None

        # A spoken number only counts as a measurement when a unit was said.
        #
        # Digits may go unitless -- "pool utilization is 40" is unambiguous --
        # but number *words* are ordinary English. "Restart one of the
        # services" contains "one", and reading that as the value 1 would
        # ground a fabricated measurement against real telemetry and produce
        # a contradiction nobody claimed. Requiring the unit costs the rare
        # "utilization is forty" and rules out the whole class.
        if unit is None:
            index += 1
            continue
        return value, unit
    return None, None


def _standalone_measurement(text: str) -> "tuple[Optional[float], Optional[str]]":
    """A number with its unit, from a fragment that names no metric."""
    match = _NUMBER_PATTERN.search(text)
    if match and match.group(2):  # a bare integer is not a measurement
        unit = match.group(2)
        return float(match.group(1)), ("%" if unit in {"%", "percent"} else unit)
    return _spoken_number(text)


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
        for sentence in self._rejoin_measurements(self._sentences(utterance), context):
            claims.extend(self._classify(sentence, context))
        claims = self._bind_carried_value(claims, utterance, context)
        substantive = [claim for claim in claims if claim.get("type") != "none"]
        return substantive or claims

    def _rejoin_measurements(
        self, sentences: list[str], context: ExtractionContext
    ) -> list[str]:
        """Put a measurement back together when speech split it in half.

        Typed, the demo's line is one sentence: "Pool utilization looks fine,
        like 40%." Spoken, the pause after "fine" is transcribed as a full
        stop, and the turn arrives as "Pool looks fine. Like forty percent."

        Split there, neither half is a measurement: the first names a metric
        with no value, the second carries a value bound to nothing. Both
        classify as bare facts, the claim is never grounded, and AEGIS cannot
        contradict a reading it never compared against.

        Deliberately narrow: the left side must name a metric *and* lack a
        value, the right side must carry a value *and* name no metric, and
        the two must be adjacent. Anything else is left exactly as it was.
        """
        metrics, aliases = context.known_metrics, context.metric_aliases
        if not metrics or len(sentences) < 2:
            return sentences

        merged: list[str] = []
        index = 0
        while index < len(sentences):
            current = sentences[index]
            if index + 1 < len(sentences):
                nxt = sentences[index + 1]
                left_metric, left_value, _ = self._match_metric(current.lower(), metrics, aliases)
                right_metric, right_value, _ = self._match_metric(nxt.lower(), metrics, aliases)
                if right_value is None:
                    # _match_metric only reads a value once a metric anchors
                    # it, so a fragment naming no metric reports none. Ask the
                    # number readers directly.
                    right_value = _standalone_measurement(nxt)[0]
                if (
                    left_metric is not None
                    and left_value is None
                    and right_metric is None
                    and right_value is not None
                ):
                    merged.append(f"{current.rstrip('.!?')}, {nxt[0].lower()}{nxt[1:]}")
                    index += 2
                    continue
            merged.append(current)
            index += 1
        return merged

    def _bind_carried_value(
        self, claims: list[dict], utterance: str, context: ExtractionContext
    ) -> list[dict]:
        """Bind a bare measurement to the metric named in the turn before it.

        The split above is the same failure one level up. Agora ends a turn on
        a pause, so the halves arrive as two independent transcript events
        seconds apart:

            06:53:18  "The pool looks fine."     -> metric, no value
            06:53:20  "Like, forty percent."     -> value, no metric

        Sentence rejoining cannot see across that boundary, so the number
        stays orphaned and the theory is never grounded.

        The binding is deliberately hard to trigger: this turn must carry a
        measurement and name no metric, no claim already made here may have
        bound one, and the *immediately* preceding turn must name a metric
        while stating no value of its own. One turn of memory, one metric, no
        search. Provenance still belongs to this utterance -- the speaker did
        say the number -- so only the metric travels.
        """
        metrics, aliases = context.known_metrics, context.metric_aliases
        if not metrics or not context.recent_turns:
            return claims
        if any(c.get("metric_ref") and c.get("claimed_value") is not None for c in claims):
            return claims

        value, unit = _standalone_measurement(utterance)
        if value is None:
            return claims
        if self._match_metric(utterance.lower(), metrics, aliases)[0] is not None:
            return claims  # this turn names its own metric; nothing to carry

        previous = context.recent_turns[-1].split(":", 1)[-1].strip().lower()
        prior_metric, prior_value, _ = self._match_metric(previous, metrics, aliases)
        if prior_metric is None or prior_value is not None:
            return claims

        # Read back as the whole remark, not the half of it this turn carried.
        # The claim is still this turn's -- provenance, speaker and timestamp
        # are untouched -- but "Like, forty percent." on its own is not a
        # theory anybody would recognise in the ledger or hear read aloud in a
        # status summary. The sentence the two turns actually formed is.
        previous_text = context.recent_turns[-1].split(":", 1)[-1].strip()
        fragment = utterance.strip()
        joined = f"{previous_text.rstrip('.!?')}, {fragment[0].lower()}{fragment[1:]}"

        carried = {
            "text": joined,
            "type": "hypothesis",
            "metric_ref": prior_metric,
            "claimed_value": value,
        }
        if unit:
            carried["claimed_unit"] = unit
        # Replaces the bare fact this turn would otherwise have produced,
        # rather than sitting alongside it as a second version of one remark.
        return [c for c in claims if c.get("type") not in ("fact", "none")] + [carried]

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

        # A hold stated when nothing is pending is a decision for the ledger,
        # whether or not the component is named. Recording it without a
        # target still matters: it is what the team agreed, and the ledger is
        # the audit trail the product is built around.
        if self._contains(lowered, _DECISION_CUES) or _HOLD_PATTERN.search(lowered):
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

        # An unhedged causal claim is still a theory, however confidently it
        # was said -- and whether or not the thing being blamed happens to be
        # a component this system knows about.
        if self._contains(lowered, _EXPLICIT_CAUSAL_CUES):
            return [{**base, "type": "hypothesis"}]
        if self._contains(lowered, _CAUSAL_CUES) and (
            target or metric or self._contains(lowered, _CERTAINTY_MARKERS)
        ):
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
        if _HOLD_PATTERN.search(lowered):
            return "hold"
        if _OVERRIDE_PATTERN.search(lowered):
            return "override"
        if self._contains(lowered, _CONFIRMATION_CUES):
            return "confirmation"
        if _BARE_AFFIRMATIVE.match(lowered.strip()):
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
        return any(cue in text for cue in cues)

    @staticmethod
    def _is_negative(text: str) -> bool:
        return bool(_NEGATIVE_STANCE_PATTERN.search(text))

    @staticmethod
    def _action_kind(text: str) -> Optional[str]:
        for kind, pattern in _ACTION_KIND_PATTERNS:
            if pattern.search(text):
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
            tail = text[anchor:]
            match = _NUMBER_PATTERN.search(tail)
            if match:
                unit = match.group(2)
                normalised_unit = "%" if unit in {"%", "percent"} else unit
                return metric, float(match.group(1)), normalised_unit
            # Digits first, words second: a transcript may carry either, and
            # "40%" is unambiguous where "forty" has to be assembled.
            spoken_value, spoken_unit = _spoken_number(tail)
            if spoken_value is not None:
                return metric, spoken_value, spoken_unit
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
