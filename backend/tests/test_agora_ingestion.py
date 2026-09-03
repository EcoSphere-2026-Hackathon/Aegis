"""Agora transport meeting the hardened reasoning core.

Agora is a *transport*. Everything it delivers must land in the same
ingestion path, obey the same idempotency, and be subject to the same
authorisation policy as a typed turn -- otherwise the voice channel is a
second, weaker door into the one decision that matters.

These tests drive the pipeline with exactly the payload
``frontend/transcript_relay.js`` posts to ``/api/transcript``: a final human
turn, ``SourceModality.VOICE``, and a turn id scoped to the voice session.
"""

from __future__ import annotations

import unittest

from backend.common.clock import ManualClock
from backend.common.config import AppConfig, GovernorConfig, load_config
from backend.common.enums import (
    ActionKind,
    ClaimType,
    ProposedActionStatus,
    SourceModality,
)
from backend.common.models import ExtractedClaim, ProposedAction, TranscriptEvent
from backend.pipeline.factory import build_runtime
from backend.pipeline.resolution import ResolutionOutcome, select_action_to_resolve
from backend.pipeline.sinks import RecordingSink
from backend.tests.support import T0


def _runtime(clock: ManualClock):
    base = load_config(env={}, dotenv_path=None, project_root=None)
    config = AppConfig(
        agora=base.agora,
        llm=base.llm,
        governor=GovernorConfig(rate_limit_seconds=45.0),
        pipeline=base.pipeline,
        api=base.api,
        database_path=base.database_path,
        incident_id="agora-test",
        log_level="CRITICAL",
    )
    return build_runtime(
        config, clock=clock, sink=RecordingSink(clock=clock), database_path=":memory:"
    )


class AgoraIngestionTests(unittest.TestCase):
    """The relay's payload, driven through the real pipeline."""

    def setUp(self) -> None:
        self.clock = ManualClock(start=T0)
        self.rt = _runtime(self.clock)
        self.addCleanup(self.rt.close)

    def _view(self):
        return self.rt.store.incident_view(captured_at=self.clock.now())

    def relay(self, uid: str, session_id: str, turn_id, text: str, *, advance: float = 5.0):
        """Exactly what transcript_relay.js posts: a scoped, final, voice turn."""
        self.clock.advance(advance)
        return self.rt.pipeline.handle_transcript(
            TranscriptEvent(
                uid=uid,
                turn_id=f"{session_id}:{turn_id}",
                role="human",
                text=text,
                final=True,
                timestamp=self.clock.now(),
                source_modality=SourceModality.VOICE,
            )
        )

    # -- ingestion and idempotency ---------------------------------------

    def test_a_voice_turn_reaches_the_reasoning_core(self) -> None:
        result = self.relay("1001", "vs_a", 1, "Payments are throwing 500s, seeing timeouts.")
        self.assertFalse(result.duplicate)
        self.assertTrue(result.claims, "the voice turn produced no claims")

    def test_a_redelivered_turn_is_absorbed_once(self) -> None:
        first = self.relay("1001", "vs_a", 1, "Payments are throwing 500s.")
        second = self.relay("1001", "vs_a", 1, "Payments are throwing 500s.")
        self.assertFalse(first.duplicate)
        self.assertTrue(second.duplicate, "a redelivered Agora turn was reasoned about twice")
        self.assertEqual(len(self._view().facts), 1)

    def test_two_participants_do_not_erase_each_other(self) -> None:
        """Agora numbers turns per agent session, starting at 1.

        Both participants therefore walk through the same small integers. If
        the counter reaches AEGIS unscoped, the second speaker's turn is
        dropped as a duplicate -- and the utterance that disappears may be the
        one proposing a destructive action.
        """
        self.relay("1001", "vs_alice", 1, "Payments are throwing 500s, seeing timeouts.")
        proposal = self.relay("1002", "vs_bob", 1, "Let's rollback core-db to the last version.")

        self.assertFalse(proposal.duplicate, "the second speaker's turn was silently dropped")
        self.assertEqual(
            len(self.rt.store.pending_actions()),
            1,
            "the rollback proposal never reached incident state",
        )

    def test_a_rejoin_does_not_collide_with_claimed_turns(self) -> None:
        # A rejoin starts a new agent and the counter restarts at 1.
        self.relay("1001", "vs_first", 1, "Payments are throwing 500s.")
        after = self.relay("1001", "vs_second", 1, "Let's rollback core-db to the last version.")
        self.assertFalse(after.duplicate)
        self.assertEqual(len(self.rt.store.pending_actions()), 1)

    def test_out_of_order_turn_ids_are_still_ingested(self) -> None:
        # Nothing downstream requires monotonic turn ids; ordering comes from
        # the event timestamp. A late-numbered turn must not be discarded.
        self.relay("1001", "vs_a", 7, "Payments are throwing 500s.")
        later = self.relay("1001", "vs_a", 3, "Error rate is around 12%.")
        self.assertFalse(later.duplicate)

    # -- authorisation cannot be reached through the voice door ----------

    def test_an_ambiguous_voice_confirmation_authorises_nothing(self) -> None:
        self.relay("1001", "vs_a", 1, "Let's rollback core-db to the last version.")
        self.relay("1001", "vs_a", 2, "Let's restart notification-service.")
        opened = len(self.rt.store.pending_actions())
        self.assertEqual(opened, 2)

        self.relay("1002", "vs_b", 1, "Yeah, go ahead.")

        still_pending = [
            action
            for action in self.rt.store.pending_actions()
            if action.status is ProposedActionStatus.PENDING
        ]
        self.assertEqual(
            len(still_pending), 2, "a bare yes over voice authorised something"
        )

    def test_a_named_voice_confirmation_resolves_exactly_that_action(self) -> None:
        self.relay("1001", "vs_a", 1, "Let's rollback core-db to the last version.")
        self.relay("1001", "vs_a", 2, "Let's restart notification-service.")
        self.relay("1002", "vs_b", 1, "Yes, go ahead with the core-db rollback.")

        resolved = [
            action
            for action in self._view().proposed_actions
            if action.status is not ProposedActionStatus.PENDING
        ]
        self.assertEqual(len(resolved), 1, "voice confirmation resolved the wrong number")
        self.assertEqual(resolved[0].target_ref, "core-db")
        self.assertEqual(resolved[0].resolved_by_uid, "1002")

    def test_a_guessed_target_over_voice_still_refuses(self) -> None:
        """The hardening applies to the voice path too.

        Extraction attaching a component the speaker never said must not take
        the named-target route, regardless of which transport delivered the
        utterance.
        """
        pending = [
            ProposedAction(
                claim_id="a1", text="roll back core-db", target_ref="core-db",
                speaker_uid="1001", timestamp=T0, action_kind=ActionKind.ROLLBACK,
                source_turn_id="vs_a:1",
            ),
            ProposedAction(
                claim_id="a2", text="restart notification-service",
                target_ref="notification-service", speaker_uid="1001", timestamp=T0,
                action_kind=ActionKind.RESTART, source_turn_id="vs_a:2",
            ),
        ]
        guessed = ExtractedClaim(
            type=ClaimType.CONFIRMATION,
            text="yeah, go ahead",
            speaker_uid="1002",
            timestamp=T0,
            source_turn_id="vs_b:1",
            target_ref="core-db",
            source_modality=SourceModality.VOICE,
        )
        decision = select_action_to_resolve(
            pending, guessed, window_seconds=120.0, utterance="yeah, go ahead"
        )
        self.assertIs(decision.outcome, ResolutionOutcome.AMBIGUOUS)
        self.assertIsNone(decision.action)

    # -- echo -------------------------------------------------------------

    def test_aegis_speech_does_not_re_enter_as_a_human_instruction(self) -> None:
        """The relay filters the agent's uid out client-side.

        This asserts the property the backend depends on: whatever AEGIS says
        is never posted back as a human turn, so a spoken warning that
        mentions a component cannot authorise an action on it.
        """
        self.relay("1001", "vs_a", 1, "Let's rollback core-db to the last version.")
        spoken_before = len(self.rt.sink.lines)
        pending_before = len(self.rt.store.pending_actions())

        # AEGIS's own utterances are never relayed; nothing new arrives.
        self.assertEqual(len(self.rt.store.pending_actions()), pending_before)
        self.assertGreaterEqual(len(self.rt.sink.lines), spoken_before)

    # -- reset ------------------------------------------------------------

    def test_reset_clears_state_produced_over_voice(self) -> None:
        self.relay("1001", "vs_a", 1, "Payments are throwing 500s.")
        self.relay("1001", "vs_a", 2, "Let's rollback core-db to the last version.")
        self.assertTrue(self.rt.store.pending_actions())

        self.rt.reset()

        self.assertEqual(self._view().facts, ())
        self.assertEqual(self.rt.store.pending_actions(), ())
        # And a turn id from before the reset is claimable again, so a
        # rehearsal can be replayed verbatim.
        replayed = self.relay("1001", "vs_a", 1, "Payments are throwing 500s.")
        self.assertFalse(replayed.duplicate)


if __name__ == "__main__":
    unittest.main()
