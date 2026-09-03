import assert from "node:assert/strict";
import test from "node:test";

import { createTranscriptRelay, normalizeFinalHumanTurn } from "./transcript_relay.js";

const localFinal = {
  uid: "0", turn_id: 41, text: " Roll back Core only after approval. ", status: 1,
  _time: 1_700_000_000,
};

const SESSION = "vs_abc123";

test("interim and agent transcript items never become AEGIS events", () => {
  assert.equal(normalizeFinalHumanTurn({ ...localFinal, status: 0 }, "1001", SESSION), null);
  assert.equal(normalizeFinalHumanTurn({ ...localFinal, uid: "9000" }, "1001", SESSION), null);
});

test("a completed local turn preserves AEGIS identity, turn ID, and timestamp", () => {
  assert.deepEqual(normalizeFinalHumanTurn(localFinal, "1001", SESSION), {
    uid: "1001", turn_id: `${SESSION}:41`, role: "human",
    text: "Roll back Core only after approval.",
    final: true, timestamp: "2023-11-14T22:13:20.000Z",
  });
});

test("a completed transcript history entry is posted once", async () => {
  const posted = [];
  const relay = createTranscriptRelay({
    participantUid: "1001", sessionId: SESSION, post: async (event) => posted.push(event),
  });
  assert.equal(await relay(localFinal), true);
  assert.equal(await relay(localFinal), false);
  assert.equal(posted.length, 1);
  assert.equal(posted[0].turn_id, `${SESSION}:41`);
});

test("failed delivery can retry without duplicating a successful event", async () => {
  let attempts = 0;
  const relay = createTranscriptRelay({
    participantUid: "1001",
    sessionId: SESSION,
    post: async () => { attempts += 1; if (attempts === 1) throw new Error("offline"); },
  });
  await assert.rejects(relay(localFinal), /offline/);
  assert.equal(await relay(localFinal), true);
  assert.equal(attempts, 2);
});

// Agora's turn counter is per agent session and starts at 1. AEGIS drops a
// repeated turn id as a duplicate, so an unscoped counter silently discards
// the second speaker's utterance -- and the one that vanishes may be the one
// proposing a rollback.
test("two participants on one incident do not collide on the same counter", () => {
  const a = normalizeFinalHumanTurn(localFinal, "1001", "vs_alice");
  const b = normalizeFinalHumanTurn({ ...localFinal, uid: "1002" }, "1002", "vs_bob");
  assert.notEqual(a.turn_id, b.turn_id);
});

test("a rejoin does not collide with turns already claimed", () => {
  const before = normalizeFinalHumanTurn({ ...localFinal, turn_id: 1 }, "1001", "vs_first");
  const after = normalizeFinalHumanTurn({ ...localFinal, turn_id: 1 }, "1001", "vs_second");
  assert.notEqual(before.turn_id, after.turn_id);
});

test("the same logical turn keeps one id, so redelivery still deduplicates", () => {
  const first = normalizeFinalHumanTurn(localFinal, "1001", SESSION);
  const again = normalizeFinalHumanTurn({ ...localFinal }, "1001", SESSION);
  assert.equal(first.turn_id, again.turn_id);
});
