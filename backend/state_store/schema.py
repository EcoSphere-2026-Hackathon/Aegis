"""
SQLite schema and migrations.

Kept separate from the repository so the shape of the data is reviewable on
its own, and so schema evolution has one obvious home.

Design notes worth stating explicitly:

* **Timestamps are ISO-8601 UTC strings with a fixed offset suffix.** That
  makes lexicographic ordering equal to chronological ordering, which is
  what lets the timeline be ordered in SQL. It is only safe because
  :func:`backend.common.clock.to_iso` normalises every timestamp to UTC
  before it is written.
* **Evidence values are stored twice, typed.** A reading of ``91`` and a
  reading of ``"v17"`` are both legitimate; storing everything as text meant
  numbers came back as strings and the engine had to re-parse them. The
  numeric and text columns keep the round trip lossless.
* **The ``evidence`` table exists from the first migration** even though
  nothing writes to it until Phase 2. Blueprint §4 c3 asks for exactly this
  so the multimodal work needs no migration later.
"""

from __future__ import annotations

import sqlite3

SCHEMA_VERSION = 1

_MIGRATION_1 = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS facts (
    claim_id        TEXT PRIMARY KEY,
    text            TEXT NOT NULL,
    speaker_uid     TEXT NOT NULL,
    timestamp       TEXT NOT NULL,
    source_turn_id  TEXT,
    source_modality TEXT NOT NULL DEFAULT 'voice'
);

CREATE TABLE IF NOT EXISTS hypotheses (
    claim_id            TEXT PRIMARY KEY,
    text                TEXT NOT NULL,
    speaker_uid         TEXT NOT NULL,
    timestamp           TEXT NOT NULL,
    status              TEXT NOT NULL,
    reinforcement_count INTEGER NOT NULL DEFAULT 0,
    last_touched_at     TEXT NOT NULL,
    target_ref          TEXT,
    metric_ref          TEXT,
    claimed_value       REAL,
    claimed_unit        TEXT,
    source_turn_id      TEXT,
    source_modality     TEXT NOT NULL DEFAULT 'voice'
);

CREATE TABLE IF NOT EXISTS decisions (
    claim_id        TEXT PRIMARY KEY,
    text            TEXT NOT NULL,
    speaker_uid     TEXT NOT NULL,
    timestamp       TEXT NOT NULL,
    target_ref      TEXT,
    stance          TEXT,
    source_turn_id  TEXT,
    source_modality TEXT NOT NULL DEFAULT 'voice'
);

CREATE TABLE IF NOT EXISTS proposed_actions (
    claim_id                 TEXT PRIMARY KEY,
    text                     TEXT NOT NULL,
    target_ref               TEXT NOT NULL,
    speaker_uid              TEXT NOT NULL,
    timestamp                TEXT NOT NULL,
    action_kind              TEXT NOT NULL,
    target_schema_version    TEXT,
    status                   TEXT NOT NULL,
    risk_verdict_json        TEXT,
    resolved_by_uid          TEXT,
    resolved_at              TEXT,
    justifying_hypothesis_id TEXT,
    source_turn_id           TEXT,
    source_modality          TEXT NOT NULL DEFAULT 'voice',
    FOREIGN KEY (justifying_hypothesis_id) REFERENCES hypotheses (claim_id)
);

CREATE TABLE IF NOT EXISTS evidence (
    evidence_id          TEXT PRIMARY KEY,
    source_type          TEXT NOT NULL,
    source               TEXT NOT NULL,
    metric_name          TEXT NOT NULL,
    value_kind           TEXT NOT NULL,
    value_numeric        REAL,
    value_text           TEXT,
    unit                 TEXT,
    extraction_certainty TEXT NOT NULL,
    uploader_uid         TEXT,
    timestamp            TEXT NOT NULL,
    target_ref           TEXT,
    raw_reference        TEXT
);

CREATE TABLE IF NOT EXISTS interventions (
    intervention_id           TEXT PRIMARY KEY,
    action                    TEXT NOT NULL,
    outcome                   TEXT NOT NULL,
    risk_tier                 TEXT NOT NULL,
    reasons_json              TEXT NOT NULL,
    codes_json                TEXT NOT NULL,
    spoken_text               TEXT,
    subject_claim_id          TEXT,
    decided_at                TEXT NOT NULL,
    rate_limit_window_open    INTEGER NOT NULL,
    seconds_since_last_spoken REAL,
    delivery_error            TEXT
);

CREATE TABLE IF NOT EXISTS timeline (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id    TEXT NOT NULL,
    collection  TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    summary     TEXT NOT NULL,
    speaker_uid TEXT,
    UNIQUE (entry_id, collection)
);

-- Read paths that run on every pipeline pass; cheap to index, and the
-- ordering guarantees below depend on them staying fast as the timeline
-- grows through a long incident.
CREATE INDEX IF NOT EXISTS idx_timeline_occurred_at   ON timeline (occurred_at, seq);
CREATE INDEX IF NOT EXISTS idx_hypotheses_status      ON hypotheses (status);
CREATE INDEX IF NOT EXISTS idx_hypotheses_metric      ON hypotheses (metric_ref);
CREATE INDEX IF NOT EXISTS idx_hypotheses_target      ON hypotheses (target_ref);
CREATE INDEX IF NOT EXISTS idx_actions_status         ON proposed_actions (status, timestamp);
CREATE INDEX IF NOT EXISTS idx_actions_target         ON proposed_actions (target_ref);
CREATE INDEX IF NOT EXISTS idx_decisions_target       ON decisions (target_ref, timestamp);
-- Covers the "current reading of each metric" window function: the
-- partition is already in ranking order, so the query is one indexed pass
-- rather than a sort per evaluation.
CREATE INDEX IF NOT EXISTS idx_evidence_metric_recent ON evidence (metric_name, timestamp DESC);

-- The reverse edge of the justification graph. When a hypothesis is
-- invalidated, this index answers "which unresolved actions rested on it"
-- without scanning every action in the incident. Partial, because the only
-- rows ever queried are the pending ones.
CREATE INDEX IF NOT EXISTS idx_actions_justification  ON proposed_actions (justifying_hypothesis_id)
    WHERE status = 'pending' AND justifying_hypothesis_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_interventions_decided  ON interventions (decided_at);

-- "When did AEGIS last raise this action out loud?" -- the reference moment
-- the confirmation policy measures a bare "go ahead" against. Partial,
-- because only interventions that actually reached the room count: a
-- suppressed or queued decision was never live in the conversation.
CREATE INDEX IF NOT EXISTS idx_interventions_subject  ON interventions (subject_claim_id, decided_at DESC)
    WHERE subject_claim_id IS NOT NULL AND spoken_text IS NOT NULL;
"""

MIGRATIONS: tuple[str, ...] = (_MIGRATION_1,)


def apply_migrations(connection: sqlite3.Connection) -> int:
    """Bring a connection's database up to :data:`SCHEMA_VERSION`.

    Uses ``PRAGMA user_version`` as the migration marker: no extra table, no
    dependency, and it is transactional along with the DDL.
    """
    current = connection.execute("PRAGMA user_version").fetchone()[0]
    if current > SCHEMA_VERSION:
        raise RuntimeError(
            f"database schema version {current} is newer than this build supports "
            f"({SCHEMA_VERSION}); refusing to downgrade"
        )
    for version in range(current, SCHEMA_VERSION):
        connection.executescript(MIGRATIONS[version])
    if current != SCHEMA_VERSION:
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    return SCHEMA_VERSION


def configure_connection(connection: sqlite3.Connection) -> None:
    """Pragmas applied to every connection.

    ``WAL`` so a reader never blocks the ingestion path; ``NORMAL`` sync
    because this is a demo-scoped store where a lost final write on power
    loss is acceptable but corruption is not; ``foreign_keys`` on because a
    dangling ``justifying_hypothesis_id`` would make a staleness finding
    reference a hypothesis that does not exist.
    """
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = NORMAL")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
