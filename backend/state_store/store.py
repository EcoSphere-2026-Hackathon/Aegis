"""
Incident State Store -- the single source of truth for a live incident.

Responsibilities and, just as importantly, non-responsibilities:

* It **stores**. It does not decide. Every staleness or risk determination
  arrives as a value computed by :mod:`backend.risk_engine`; this module
  applies it (Blueprint §4 c3).
* It **guards state transitions**. A ``proposed_action`` leaves ``pending``
  exactly once, through an atomic conditional update. Re-resolving one is an
  error, not a silent overwrite -- silently changing a prior human decision
  is Quality Standard §4 red line #5, and "we simply never wrote that bug"
  is a weaker guarantee than "the database will not let us".
* It is **thread-safe and transactional**. Transcript ingestion, the
  pipeline worker and the UI read path all touch it concurrently.

Concurrency design: one connection, serialised by a reentrant lock. With a
handful of writes per minute there is no throughput argument for a
connection pool, and a single writer makes "was this snapshot torn across a
concurrent write?" a question that cannot arise. WAL is still enabled so a
crash mid-incident loses at most the last transaction.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Optional, Sequence

from backend.common.clock import to_iso
from backend.common.enums import (
    ActionKind,
    DecisionStance,
    EvidenceSource,
    EvidenceSourceType,
    ExtractionCertainty,
    GovernorAction,
    HypothesisStatus,
    InterventionOutcome,
    ProposedActionStatus,
    RiskFindingCode,
    RiskTier,
    SourceModality,
)
from backend.common.errors import EntityNotFoundError, IllegalStateTransitionError
from backend.common.logging import STAGE_STATE_MUTATED, get_logger
from backend.common.models import (
    Decision,
    Evidence,
    Fact,
    Hypothesis,
    IncidentView,
    InterventionRecord,
    ProposedAction,
    RiskVerdict,
    StateSnapshot,
    TimelineEntry,
)
from backend.state_store.schema import apply_migrations, configure_connection

_log = get_logger("state_store")

#: Transitions a proposed action may make. Any other pairing is an error.
_ALLOWED_ACTION_TRANSITIONS: frozenset[tuple[ProposedActionStatus, ProposedActionStatus]] = frozenset(
    {
        (ProposedActionStatus.PENDING, ProposedActionStatus.CONFIRMED),
        (ProposedActionStatus.PENDING, ProposedActionStatus.DECLINED),
        (ProposedActionStatus.PENDING, ProposedActionStatus.HELD),
    }
)


class IncidentStateStore:
    """Repository over one incident's SQLite database."""

    def __init__(self, database_path: Path | str = ":memory:", *, incident_id: str = "incident-local") -> None:
        self._incident_id = incident_id
        self._lock = threading.RLock()
        # Bumped on every successful mutation. The console re-reads state
        # after each event, and almost all of those reads return something it
        # already has; a version counter turns them into 304s instead of a
        # full re-serialisation of the incident.
        self._version = 0
        # Turn ids already ingested, so a retried delivery is a no-op rather
        # than a second copy of the same utterance.
        self._seen_turn_ids: set[str] = set()

        path = str(database_path)
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)

        # isolation_level=None puts transaction control in our hands rather
        # than sqlite3's implicit-commit heuristics, which is what makes
        # multi-statement mutations genuinely atomic.
        self._connection = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._connection.row_factory = sqlite3.Row

        with self._lock:
            configure_connection(self._connection)
            apply_migrations(self._connection)
            self._connection.execute(
                "INSERT OR IGNORE INTO meta (key, value) VALUES ('incident_id', ?)",
                (incident_id,),
            )

    # -- lifecycle --------------------------------------------------------

    @property
    def incident_id(self) -> str:
        return self._incident_id

    @property
    def version(self) -> int:
        """Monotonic state version, cheap enough to read on every request."""
        with self._lock:
            return self._version

    def claim_turn(self, turn_id: str) -> bool:
        """Reserve a turn id for processing.

        Returns False if this turn has already been ingested. Transport
        retries are normal -- a browser relaying RTM resends on a dropped
        response -- and without this the same utterance becomes two claims
        with *different* ids, which no downstream idempotency can collapse
        precisely because the ids genuinely differ. Deduplicating has to
        happen at the boundary, on the one identifier the transport
        preserves.
        """
        with self._lock:
            if turn_id in self._seen_turn_ids:
                return False
            self._seen_turn_ids.add(turn_id)
            return True

    def reset_incident(self) -> None:
        """Empty the incident, keeping the schema.

        Exists for one reason and it is not tidiness: the demo has to be
        runnable twice. The database is a file by default, so a second run
        inherits the first one's pending actions, stale theories and spent
        turn ids -- which is exactly the state in which the rehearsed script
        stops behaving as rehearsed, in front of whoever is watching.

        Deleting rows rather than the file keeps the schema, the migrations
        and any open connection valid, so this is safe to call on a live
        server between takes.
        """
        with self._transaction() as conn:
            for table in (
                "timeline",
                "interventions",
                "evidence",
                "proposed_actions",
                "decisions",
                "hypotheses",
                "facts",
            ):
                conn.execute(f"DELETE FROM {table}")
        with self._lock:
            self._seen_turn_ids.clear()
        _log.info("incident state reset", stage=STAGE_STATE_MUTATED)

    def turn_seen(self, turn_id: str) -> bool:
        """Has this turn already been claimed? Read-only.

        For callers that want to *report* a duplicate without consuming the
        claim -- the HTTP layer answering 200-vs-202 before the turn is even
        queued. The authoritative claim belongs to the pipeline, so this
        deliberately does not reserve anything: two racing requests can both
        see "new", and the pipeline's claim serialises them.
        """
        with self._lock:
            return turn_id in self._seen_turn_ids

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> "IncidentStateStore":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- transactions -----------------------------------------------------

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        """One atomic unit of work.

        ``BEGIN IMMEDIATE`` takes the write lock up front rather than on
        first write, which turns a potential mid-transaction "database is
        locked" into a clean wait at the boundary.
        """
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
            except Exception:
                self._connection.execute("ROLLBACK")
                raise
            else:
                self._connection.execute("COMMIT")
                # The version is bumped here rather than at each call site,
                # so no future mutation can forget to invalidate the readers'
                # caches. A transaction that turned out to be a no-op (a
                # duplicate INSERT OR IGNORE) still bumps, which makes this an
                # over-invalidating validator: it can cost a redundant re-read,
                # but it can never report "unchanged" when something changed.
                # For a cache validator that is the only safe direction to err.
                self._version += 1

    @contextmanager
    def _read_transaction(self) -> Iterator[sqlite3.Connection]:
        """A consistent multi-statement read.

        A snapshot assembled from five unsynchronised queries can observe
        half of a concurrent write -- a proposed action present but the
        hypothesis justifying it absent. Wrapping the reads makes the view
        internally consistent.
        """
        with self._lock:
            self._connection.execute("BEGIN")
            try:
                yield self._connection
            finally:
                self._connection.execute("COMMIT")

    # -- writes -----------------------------------------------------------

    def add_fact(self, fact: Fact) -> bool:
        """Returns True if stored, False if this claim_id was already known.

        Idempotent because RTM can redeliver an event, and a redelivered
        utterance must not become two facts.
        """
        with self._transaction() as conn:
            cursor = conn.execute(
                """INSERT OR IGNORE INTO facts
                   (claim_id, text, speaker_uid, timestamp, source_turn_id, source_modality)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    fact.claim_id,
                    fact.text,
                    fact.speaker_uid,
                    to_iso(fact.timestamp),
                    fact.source_turn_id,
                    fact.source_modality.value,
                ),
            )
            inserted = cursor.rowcount > 0
            if inserted:
                self._append_timeline(conn, fact.claim_id, "facts", fact.timestamp, fact.text, fact.speaker_uid)
        if inserted:
            _log.info("fact recorded", stage=STAGE_STATE_MUTATED, claim_id=fact.claim_id,
                      collection="facts", speaker_uid=fact.speaker_uid)
        return inserted

    def add_hypothesis(self, hypothesis: Hypothesis) -> bool:
        with self._transaction() as conn:
            cursor = conn.execute(
                """INSERT OR IGNORE INTO hypotheses
                   (claim_id, text, speaker_uid, timestamp, status, reinforcement_count,
                    last_touched_at, target_ref, metric_ref, claimed_value, claimed_unit,
                    source_turn_id, source_modality)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    hypothesis.claim_id,
                    hypothesis.text,
                    hypothesis.speaker_uid,
                    to_iso(hypothesis.timestamp),
                    hypothesis.status.value,
                    hypothesis.reinforcement_count,
                    to_iso(hypothesis.touched_at),
                    hypothesis.target_ref,
                    hypothesis.metric_ref,
                    hypothesis.claimed_value,
                    hypothesis.claimed_unit,
                    hypothesis.source_turn_id,
                    hypothesis.source_modality.value,
                ),
            )
            inserted = cursor.rowcount > 0
            if inserted:
                self._append_timeline(
                    conn, hypothesis.claim_id, "hypotheses", hypothesis.timestamp,
                    hypothesis.text, hypothesis.speaker_uid,
                )
        if inserted:
            _log.info("hypothesis recorded", stage=STAGE_STATE_MUTATED, claim_id=hypothesis.claim_id,
                      collection="hypotheses", metric_ref=hypothesis.metric_ref,
                      claimed_value=hypothesis.claimed_value)
        return inserted

    def add_decision(self, decision: Decision) -> bool:
        with self._transaction() as conn:
            cursor = conn.execute(
                """INSERT OR IGNORE INTO decisions
                   (claim_id, text, speaker_uid, timestamp, target_ref, stance,
                    source_turn_id, source_modality)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    decision.claim_id,
                    decision.text,
                    decision.speaker_uid,
                    to_iso(decision.timestamp),
                    decision.target_ref,
                    decision.stance.value if decision.stance else None,
                    decision.source_turn_id,
                    decision.source_modality.value,
                ),
            )
            inserted = cursor.rowcount > 0
            if inserted:
                self._append_timeline(
                    conn, decision.claim_id, "decisions", decision.timestamp,
                    decision.text, decision.speaker_uid,
                )
        if inserted:
            _log.info("decision recorded", stage=STAGE_STATE_MUTATED, claim_id=decision.claim_id,
                      collection="decisions", target_ref=decision.target_ref,
                      stance=decision.stance.value if decision.stance else None)
        return inserted

    def add_proposed_action(self, action: ProposedAction) -> bool:
        with self._transaction() as conn:
            cursor = conn.execute(
                """INSERT OR IGNORE INTO proposed_actions
                   (claim_id, text, target_ref, speaker_uid, timestamp, action_kind,
                    target_schema_version, status, risk_verdict_json, resolved_by_uid,
                    resolved_at, justifying_hypothesis_id, source_turn_id, source_modality)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    action.claim_id,
                    action.text,
                    action.target_ref,
                    action.speaker_uid,
                    to_iso(action.timestamp),
                    action.action_kind.value,
                    action.target_schema_version,
                    action.status.value,
                    action.risk_verdict.model_dump_json() if action.risk_verdict else None,
                    action.resolved_by_uid,
                    to_iso(action.resolved_at) if action.resolved_at else None,
                    action.justifying_hypothesis_id,
                    action.source_turn_id,
                    action.source_modality.value,
                ),
            )
            inserted = cursor.rowcount > 0
            if inserted:
                self._append_timeline(
                    conn, action.claim_id, "proposed_actions", action.timestamp,
                    action.text, action.speaker_uid,
                )
        if inserted:
            _log.info("proposed action recorded", stage=STAGE_STATE_MUTATED, claim_id=action.claim_id,
                      collection="proposed_actions", target_ref=action.target_ref,
                      action_kind=action.action_kind.value)
        return inserted

    def add_evidence(self, evidence: Evidence) -> bool:
        numeric = evidence.numeric_value if not isinstance(evidence.value, str) else None
        if isinstance(evidence.value, str):
            value_kind, value_text = "text", evidence.value
        else:
            value_kind, value_text = "numeric", None
            numeric = float(evidence.value)

        with self._transaction() as conn:
            cursor = conn.execute(
                """INSERT OR IGNORE INTO evidence
                   (evidence_id, source_type, source, metric_name, value_kind, value_numeric,
                    value_text, unit, extraction_certainty, uploader_uid, timestamp,
                    target_ref, raw_reference)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    evidence.evidence_id,
                    evidence.source_type.value,
                    evidence.source.value,
                    evidence.metric_name,
                    value_kind,
                    numeric,
                    value_text,
                    evidence.unit,
                    evidence.extraction_certainty.value,
                    evidence.uploader_uid,
                    to_iso(evidence.timestamp),
                    evidence.target_ref,
                    evidence.raw_reference,
                ),
            )
            inserted = cursor.rowcount > 0
            if inserted:
                summary = f"{evidence.metric_name} = {evidence.value}{evidence.unit or ''}"
                self._append_timeline(
                    conn, evidence.evidence_id, "evidence", evidence.timestamp,
                    summary, evidence.uploader_uid,
                )
        if inserted:
            _log.info("evidence recorded", stage=STAGE_STATE_MUTATED,
                      evidence_id=evidence.evidence_id, collection="evidence",
                      metric=evidence.metric_name, source=evidence.source.value)
        return inserted

    def record_intervention(self, record: InterventionRecord) -> bool:
        with self._transaction() as conn:
            cursor = conn.execute(
                """INSERT OR IGNORE INTO interventions
                   (intervention_id, action, outcome, risk_tier, reasons_json, codes_json,
                    spoken_text, subject_claim_id, decided_at, rate_limit_window_open,
                    seconds_since_last_spoken, delivery_error)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record.intervention_id,
                    record.action.value,
                    record.outcome.value,
                    record.risk_tier.value,
                    json.dumps(list(record.reasons)),
                    json.dumps([code.value for code in record.codes]),
                    record.spoken_text,
                    record.subject_claim_id,
                    to_iso(record.decided_at),
                    1 if record.rate_limit_window_open else 0,
                    record.seconds_since_last_spoken,
                    record.delivery_error,
                ),
            )
            return cursor.rowcount > 0

    # -- guarded state transitions ---------------------------------------

    def attach_risk_verdict(self, action_claim_id: str, verdict: RiskVerdict) -> None:
        """Record the engine's verdict against an action.

        Attaching a verdict is explicitly *not* a status change: a HIGH-risk
        action is still ``pending`` until a human resolves it.
        """
        with self._transaction() as conn:
            cursor = conn.execute(
                "UPDATE proposed_actions SET risk_verdict_json = ? WHERE claim_id = ?",
                (verdict.model_dump_json(), action_claim_id),
            )
            if cursor.rowcount == 0:
                raise EntityNotFoundError(
                    "cannot attach a verdict to an unknown proposed action",
                    claim_id=action_claim_id,
                )

    def resolve_proposed_action(
        self,
        action_claim_id: str,
        status: ProposedActionStatus,
        *,
        resolved_by_uid: str,
        resolved_at: datetime,
    ) -> ProposedAction:
        """The only path out of ``pending``.

        The update is conditional on the row still being ``pending``, so two
        confirmations arriving concurrently cannot both win: the second sees
        ``rowcount == 0`` and raises rather than overwriting the first
        human's decision.

        Callers must have an explicit, classified human resolution claim in
        hand. Silence, ambiguity and timeouts never reach here.
        """
        if status is ProposedActionStatus.PENDING:
            raise IllegalStateTransitionError(
                "pending is the initial state, not a resolution",
                claim_id=action_claim_id,
            )
        if not resolved_by_uid:
            raise IllegalStateTransitionError(
                "a resolution must record which human made it",
                claim_id=action_claim_id,
            )

        with self._transaction() as conn:
            row = conn.execute(
                "SELECT status FROM proposed_actions WHERE claim_id = ?", (action_claim_id,)
            ).fetchone()
            if row is None:
                raise EntityNotFoundError("unknown proposed action", claim_id=action_claim_id)

            current = ProposedActionStatus(row["status"])
            if (current, status) not in _ALLOWED_ACTION_TRANSITIONS:
                raise IllegalStateTransitionError(
                    "proposed action has already been resolved by a human",
                    claim_id=action_claim_id,
                    current_status=current.value,
                    attempted_status=status.value,
                )

            cursor = conn.execute(
                """UPDATE proposed_actions
                   SET status = ?, resolved_by_uid = ?, resolved_at = ?
                   WHERE claim_id = ? AND status = ?""",
                (
                    status.value,
                    resolved_by_uid,
                    to_iso(resolved_at),
                    action_claim_id,
                    ProposedActionStatus.PENDING.value,
                ),
            )
            if cursor.rowcount == 0:
                # Lost a race between the SELECT and the UPDATE.
                raise IllegalStateTransitionError(
                    "proposed action was resolved concurrently",
                    claim_id=action_claim_id,
                    attempted_status=status.value,
                )
            updated = self._row_to_action(
                conn.execute(
                    "SELECT * FROM proposed_actions WHERE claim_id = ?", (action_claim_id,)
                ).fetchone()
            )

        _log.info(
            "proposed action resolved by human",
            stage=STAGE_STATE_MUTATED,
            claim_id=action_claim_id,
            status=status.value,
            resolved_by_uid=resolved_by_uid,
        )
        return updated

    def apply_hypothesis_transitions(self, transitions: Any, *, touched_at: datetime) -> None:
        """Apply a determination produced by :mod:`backend.risk_engine.staleness`.

        Applied in one transaction: a crash must not leave one hypothesis
        stale and its counterpart un-reinforced.
        """
        stale_ids = tuple(getattr(transitions, "stale_claim_ids", ()))
        reinforced_ids = tuple(getattr(transitions, "reinforced_claim_ids", ()))
        if not stale_ids and not reinforced_ids:
            return

        with self._transaction() as conn:
            for claim_id in stale_ids:
                conn.execute(
                    "UPDATE hypotheses SET status = ?, last_touched_at = ? WHERE claim_id = ? AND status = ?",
                    (
                        HypothesisStatus.STALE.value,
                        to_iso(touched_at),
                        claim_id,
                        HypothesisStatus.ACTIVE.value,
                    ),
                )
            for claim_id in reinforced_ids:
                conn.execute(
                    """UPDATE hypotheses
                       SET reinforcement_count = reinforcement_count + 1, last_touched_at = ?
                       WHERE claim_id = ? AND status = ?""",
                    (to_iso(touched_at), claim_id, HypothesisStatus.ACTIVE.value),
                )

        _log.info(
            "hypothesis transitions applied",
            stage=STAGE_STATE_MUTATED,
            stale=list(stale_ids),
            reinforced=list(reinforced_ids),
        )

    def link_justifying_hypothesis(self, action_claim_id: str, hypothesis_claim_id: Optional[str]) -> None:
        with self._transaction() as conn:
            cursor = conn.execute(
                "UPDATE proposed_actions SET justifying_hypothesis_id = ? WHERE claim_id = ?",
                (hypothesis_claim_id, action_claim_id),
            )
            if cursor.rowcount == 0:
                raise EntityNotFoundError("unknown proposed action", claim_id=action_claim_id)

    # -- reads ------------------------------------------------------------

    def get_hypothesis(self, claim_id: str) -> Optional[Hypothesis]:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM hypotheses WHERE claim_id = ?", (claim_id,)
            ).fetchone()
        return self._row_to_hypothesis(row) if row else None

    def get_proposed_action(self, claim_id: str) -> Optional[ProposedAction]:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM proposed_actions WHERE claim_id = ?", (claim_id,)
            ).fetchone()
        return self._row_to_action(row) if row else None

    def active_hypotheses(self) -> tuple[Hypothesis, ...]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM hypotheses WHERE status = ? ORDER BY timestamp",
                (HypothesisStatus.ACTIVE.value,),
            ).fetchall()
        return tuple(self._row_to_hypothesis(row) for row in rows)

    def pending_actions(self) -> tuple[ProposedAction, ...]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM proposed_actions WHERE status = ? ORDER BY timestamp",
                (ProposedActionStatus.PENDING.value,),
            ).fetchall()
        return tuple(self._row_to_action(row) for row in rows)

    def evidence(self) -> tuple[Evidence, ...]:
        with self._lock:
            rows = self._connection.execute("SELECT * FROM evidence ORDER BY timestamp").fetchall()
        return tuple(self._row_to_evidence(row) for row in rows)

    def interventions(self) -> tuple[InterventionRecord, ...]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM interventions ORDER BY decided_at"
            ).fetchall()
        return tuple(self._row_to_intervention(row) for row in rows)

    def timeline(self) -> tuple[TimelineEntry, ...]:
        """Chronological, not write-ordered.

        RTM does not guarantee ordered delivery, so insertion order is not
        event order. ``seq`` breaks ties deterministically for events that
        share a timestamp.
        """
        with self._lock:
            rows = self._connection.execute(
                "SELECT entry_id, collection, occurred_at, summary, speaker_uid "
                "FROM timeline ORDER BY occurred_at, seq"
            ).fetchall()
        return tuple(
            TimelineEntry(
                entry_id=row["entry_id"],
                collection=row["collection"],
                occurred_at=datetime.fromisoformat(row["occurred_at"]),
                summary=row["summary"],
                speaker_uid=row["speaker_uid"],
            )
            for row in rows
        )

    def pending_actions_justified_by(self, hypothesis_ids: Sequence[str]) -> tuple[ProposedAction, ...]:
        """Which unresolved actions rest on these theories?

        The reverse edge of the justification graph. When reality invalidates
        a hypothesis, this is the query that answers "what did we conclude
        from it that now needs revisiting" -- and it is an indexed lookup
        rather than a scan over every action in the incident.
        """
        if not hypothesis_ids:
            return ()
        placeholders = ",".join("?" for _ in hypothesis_ids)
        with self._lock:
            rows = self._connection.execute(
                f"""SELECT * FROM proposed_actions
                    WHERE status = ? AND justifying_hypothesis_id IN ({placeholders})
                    ORDER BY timestamp""",
                (ProposedActionStatus.PENDING.value, *hypothesis_ids),
            ).fetchall()
        return tuple(self._row_to_action(row) for row in rows)

    def recently_resolved(
        self,
        *,
        target_ref: str,
        action_kind: ActionKind,
        since: datetime,
    ) -> Optional[ProposedAction]:
        """A matching action a human already decided, within the window.

        Exists to recognise an *echo*: a second person agreeing with an
        approval that has just been given. The words are indistinguishable
        from a fresh proposal -- "yes, roll back core-db" is both -- and the
        difference is entirely in the state, so the state layer is where it
        has to be settled. Recording the echo as a new pending action
        re-opens a settled question, and the console shows the same rollback
        as confirmed and pending at once.

        **Only a confirmation counts.** Re-proposing something the room *held*
        or *declined* is not an echo -- it is a decision reversal, which is
        one of the four things this product exists to catch. Matching on any
        terminal status instead of CONFIRMED silently disables that check for
        the whole window, which is the most expensive possible way to be
        tidy. (Caught by the benchmark: risk evaluations per run dropped and
        the reversal scenario stopped firing.)
        """
        with self._lock:
            row = self._connection.execute(
                """SELECT * FROM proposed_actions
                   WHERE target_ref = ? AND action_kind = ? AND status = ?
                     AND resolved_at >= ?
                   ORDER BY resolved_at DESC LIMIT 1""",
                (
                    target_ref,
                    action_kind.value,
                    ProposedActionStatus.CONFIRMED.value,
                    to_iso(since),
                ),
            ).fetchone()
        return self._row_to_action(row) if row is not None else None

    def hypotheses_for_association(
        self,
        *,
        target_ref: Optional[str],
        metric_refs: Sequence[str] = (),
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
    ) -> tuple[Hypothesis, ...]:
        """Theories that could plausibly justify an action on ``target_ref``.

        Association needs three narrow slices -- theories about this
        component, theories about a metric that describes it, and anything
        stated recently -- and it needed all of them at once, so it read the
        entire incident snapshot on every proposed action. That is the same
        O(incident) read the working set exists to avoid, left on the hot
        path.

        One indexed union answers all three. Stale theories are deliberately
        included: an action resting on a theory reality already contradicted
        is precisely the failure this product exists to catch, so filtering
        to active hypotheses here would hide it.
        """
        clauses: list[str] = []
        params: list[object] = []
        if target_ref:
            clauses.append("target_ref = ?")
            params.append(target_ref)
        if metric_refs:
            placeholders = ",".join("?" for _ in metric_refs)
            clauses.append(f"metric_ref IN ({placeholders})")
            params.extend(metric_refs)
        if since is not None:
            clauses.append("timestamp >= ?")
            params.append(to_iso(since))
        if not clauses:
            return ()

        sql = f"SELECT * FROM hypotheses WHERE ({' OR '.join(clauses)})"
        if until is not None:
            sql += " AND timestamp <= ?"
            params.append(to_iso(until))
        sql += " ORDER BY timestamp"

        with self._lock:
            rows = self._connection.execute(sql, params).fetchall()
        return tuple(self._row_to_hypothesis(row) for row in rows)

    def last_raised_at(self, claim_ids: Sequence[str]) -> dict[str, datetime]:
        """When AEGIS last said something out loud about each of these actions.

        The confirmation policy needs a reference moment for "is this reply
        still about that action". Proposal time alone is the wrong one: a
        discussion AEGIS itself re-opened is exactly when a human finally
        answers, and timing that answer out would be perverse.

        Only interventions that reached the room count. A decision that was
        suppressed as a duplicate or queued behind the rate limit was never
        live in the conversation, so it cannot make a reply about it timely.
        """
        if not claim_ids:
            return {}
        placeholders = ",".join("?" for _ in claim_ids)
        with self._lock:
            rows = self._connection.execute(
                f"""SELECT subject_claim_id, MAX(decided_at) AS raised_at
                    FROM interventions
                    WHERE subject_claim_id IN ({placeholders})
                      AND spoken_text IS NOT NULL
                      AND outcome = ?
                    GROUP BY subject_claim_id""",
                (*claim_ids, InterventionOutcome.SPOKEN.value),
            ).fetchall()
        return {
            row["subject_claim_id"]: datetime.fromisoformat(row["raised_at"])
            for row in rows
            if row["subject_claim_id"] and row["raised_at"]
        }

    def latest_evidence_per_metric(self) -> tuple[Evidence, ...]:
        """The current reading of each metric, resolved in SQL.

        Every risk evaluation needs "what does each metric say *now*". Doing
        that by loading the whole evidence history and folding it in Python
        is O(readings) per evaluation, and readings accumulate for as long as
        the incident runs -- so the cost of answering a fixed-size question
        grows without bound.

        A window function answers it in one indexed pass and returns exactly
        one row per metric. The index on ``(metric_name, timestamp DESC)``
        means the partition is already in the order the ranking needs.
        """
        with self._lock:
            rows = self._connection.execute(
                """SELECT * FROM (
                       SELECT *, ROW_NUMBER() OVER (
                           PARTITION BY metric_name ORDER BY timestamp DESC, rowid DESC
                       ) AS recency
                       FROM evidence
                   ) WHERE recency = 1
                   ORDER BY metric_name"""
            ).fetchall()
        return tuple(self._row_to_evidence(row) for row in rows)

    def working_set_for(
        self, action: ProposedAction, *, captured_at: datetime
    ) -> StateSnapshot:
        """Exactly the state one risk evaluation reads -- nothing else.

        The checks need one hypothesis (the action's justification) and the
        decisions recorded against one target. Materialising the entire
        incident to answer that was the single most wasteful thing this
        service did: O(claims) rows fetched and Pydantic-validated per
        evaluation, repeated on every proposed action, to read two of them.

        Both lookups are covered by indexes, so this is O(result) instead of
        O(incident) and stays flat as the conversation grows.
        """
        with self._read_transaction() as conn:
            hypotheses: list[Hypothesis] = []
            if action.justifying_hypothesis_id:
                row = conn.execute(
                    "SELECT * FROM hypotheses WHERE claim_id = ?",
                    (action.justifying_hypothesis_id,),
                ).fetchone()
                if row is not None:
                    hypotheses.append(self._row_to_hypothesis(row))

            decision_rows = conn.execute(
                # Only the most recent decision at or before the action's
                # moment is consulted, so fetching the target's whole
                # decision history reintroduces exactly the O(incident)
                # growth the working set exists to remove -- and it grows
                # fastest on the target the room is arguing about, which is
                # the one that matters.
                """SELECT * FROM (
                       SELECT * FROM decisions
                       WHERE target_ref = ? AND timestamp <= ?
                       ORDER BY timestamp DESC LIMIT 1
                   ) ORDER BY timestamp""",
                (action.target_ref, to_iso(action.timestamp)),
            ).fetchall()

        return StateSnapshot(
            captured_at=captured_at,
            facts=(),
            hypotheses=tuple(hypotheses),
            decisions=tuple(self._row_to_decision(row) for row in decision_rows),
            proposed_actions=(action,),
        )

    def snapshot(self, *, captured_at: datetime) -> StateSnapshot:
        """The engine's input contract, read consistently."""
        with self._read_transaction() as conn:
            facts = conn.execute("SELECT * FROM facts ORDER BY timestamp").fetchall()
            hypotheses = conn.execute("SELECT * FROM hypotheses ORDER BY timestamp").fetchall()
            decisions = conn.execute("SELECT * FROM decisions ORDER BY timestamp").fetchall()
            actions = conn.execute("SELECT * FROM proposed_actions ORDER BY timestamp").fetchall()
        return StateSnapshot(
            captured_at=captured_at,
            facts=tuple(self._row_to_fact(r) for r in facts),
            hypotheses=tuple(self._row_to_hypothesis(r) for r in hypotheses),
            decisions=tuple(self._row_to_decision(r) for r in decisions),
            proposed_actions=tuple(self._row_to_action(r) for r in actions),
        )

    def incident_view(self, *, captured_at: datetime) -> IncidentView:
        """The presentation projection: everything, including evidence,
        interventions and the timeline."""
        snapshot = self.snapshot(captured_at=captured_at)
        return IncidentView(
            incident_id=self._incident_id,
            captured_at=captured_at,
            facts=snapshot.facts,
            hypotheses=snapshot.hypotheses,
            decisions=snapshot.decisions,
            proposed_actions=snapshot.proposed_actions,
            evidence=self.evidence(),
            interventions=self.interventions(),
            timeline=self.timeline(),
        )

    # -- internals --------------------------------------------------------

    def _append_timeline(
        self,
        conn: sqlite3.Connection,
        entry_id: str,
        collection: str,
        occurred_at: datetime,
        summary: str,
        speaker_uid: Optional[str],
    ) -> None:
        conn.execute(
            """INSERT OR IGNORE INTO timeline
               (entry_id, collection, occurred_at, recorded_at, summary, speaker_uid)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                entry_id,
                collection,
                to_iso(occurred_at),
                to_iso(occurred_at),
                summary[:500],
                speaker_uid,
            ),
        )

    @staticmethod
    def _row_to_fact(row: sqlite3.Row) -> Fact:
        return Fact(
            claim_id=row["claim_id"],
            text=row["text"],
            speaker_uid=row["speaker_uid"],
            timestamp=datetime.fromisoformat(row["timestamp"]),
            source_turn_id=row["source_turn_id"],
            source_modality=SourceModality(row["source_modality"]),
        )

    @staticmethod
    def _row_to_hypothesis(row: sqlite3.Row) -> Hypothesis:
        return Hypothesis(
            claim_id=row["claim_id"],
            text=row["text"],
            speaker_uid=row["speaker_uid"],
            timestamp=datetime.fromisoformat(row["timestamp"]),
            status=HypothesisStatus(row["status"]),
            reinforcement_count=row["reinforcement_count"],
            last_touched_at=datetime.fromisoformat(row["last_touched_at"]),
            target_ref=row["target_ref"],
            metric_ref=row["metric_ref"],
            claimed_value=row["claimed_value"],
            claimed_unit=row["claimed_unit"],
            source_turn_id=row["source_turn_id"],
            source_modality=SourceModality(row["source_modality"]),
        )

    @staticmethod
    def _row_to_decision(row: sqlite3.Row) -> Decision:
        return Decision(
            claim_id=row["claim_id"],
            text=row["text"],
            speaker_uid=row["speaker_uid"],
            timestamp=datetime.fromisoformat(row["timestamp"]),
            target_ref=row["target_ref"],
            stance=DecisionStance(row["stance"]) if row["stance"] else None,
            source_turn_id=row["source_turn_id"],
            source_modality=SourceModality(row["source_modality"]),
        )

    @staticmethod
    def _row_to_action(row: sqlite3.Row) -> ProposedAction:
        verdict = (
            RiskVerdict.model_validate_json(row["risk_verdict_json"])
            if row["risk_verdict_json"]
            else None
        )
        return ProposedAction(
            claim_id=row["claim_id"],
            text=row["text"],
            target_ref=row["target_ref"],
            speaker_uid=row["speaker_uid"],
            timestamp=datetime.fromisoformat(row["timestamp"]),
            action_kind=ActionKind(row["action_kind"]),
            target_schema_version=row["target_schema_version"],
            status=ProposedActionStatus(row["status"]),
            risk_verdict=verdict,
            resolved_by_uid=row["resolved_by_uid"],
            resolved_at=datetime.fromisoformat(row["resolved_at"]) if row["resolved_at"] else None,
            justifying_hypothesis_id=row["justifying_hypothesis_id"],
            source_turn_id=row["source_turn_id"],
            source_modality=SourceModality(row["source_modality"]),
        )

    @staticmethod
    def _row_to_evidence(row: sqlite3.Row) -> Evidence:
        value: float | str
        if row["value_kind"] == "numeric":
            value = float(row["value_numeric"])
        else:
            value = row["value_text"] or ""
        return Evidence(
            evidence_id=row["evidence_id"],
            source_type=EvidenceSourceType(row["source_type"]),
            source=EvidenceSource(row["source"]),
            metric_name=row["metric_name"],
            value=value,
            unit=row["unit"],
            extraction_certainty=ExtractionCertainty(row["extraction_certainty"]),
            uploader_uid=row["uploader_uid"],
            timestamp=datetime.fromisoformat(row["timestamp"]),
            target_ref=row["target_ref"],
            raw_reference=row["raw_reference"],
        )

    @staticmethod
    def _row_to_intervention(row: sqlite3.Row) -> InterventionRecord:
        return InterventionRecord(
            intervention_id=row["intervention_id"],
            action=GovernorAction(row["action"]),
            outcome=InterventionOutcome(row["outcome"]),
            risk_tier=RiskTier(row["risk_tier"]),
            reasons=tuple(json.loads(row["reasons_json"])),
            codes=tuple(RiskFindingCode(code) for code in json.loads(row["codes_json"])),
            spoken_text=row["spoken_text"],
            subject_claim_id=row["subject_claim_id"],
            decided_at=datetime.fromisoformat(row["decided_at"]),
            rate_limit_window_open=bool(row["rate_limit_window_open"]),
            seconds_since_last_spoken=row["seconds_since_last_spoken"],
            delivery_error=row["delivery_error"],
        )
