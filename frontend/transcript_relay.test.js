import assert from "node:assert/strict";
import test from "node:test";

import { createTranscriptRelay, normalizeFinalHumanTurn } from "./transcript_relay.js";

const localFinal = {
  uid: "0", turn_id: 41, text: " Roll back Core only after approval. ", status: 1,
  _time: 1_700_000_000,
};

test("interim and agent transcript items never become AEGIS events", () => {
  assert.equal(normalizeFinalHumanTurn({ ...localFinal, status: 0 }, "1001"), null);
  assert.equal(normalizeFinalHumanTurn({ ...localFinal, uid: "9000" }, "1001"), null);
});

test("a completed local turn preserves AEGIS identity, turn ID, and timestamp", () => {
  assert.deepEqual(normalizeFinalHumanTurn(localFinal, "1001"), {
    uid: "1001", turn_id: "41", role: "human", text: "Roll back Core only after approval.",
    final: true, timestamp: "2023-11-14T22:13:20.000Z",
  });
});

test("a completed transcript history entry is posted once", async () => {
  const posted = [];
  const relay = createTranscriptRelay({ participantUid: "1001", post: async (event) => posted.push(event) });
  assert.equal(await relay(localFinal), true);
  assert.equal(await relay(localFinal), false);
  assert.equal(posted.length, 1);
  assert.equal(posted[0].turn_id, "41");
});

test("failed delivery can retry without duplicating a successful event", async () => {
  let attempts = 0;
  const relay = createTranscriptRelay({
    participantUid: "1001",
    post: async () => { attempts += 1; if (attempts === 1) throw new Error("offline"); },
  });
  await assert.rejects(relay(localFinal), /offline/);
  assert.equal(await relay(localFinal), true);
  assert.equal(attempts, 2);
});
