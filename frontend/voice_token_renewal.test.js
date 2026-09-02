import assert from "node:assert/strict";
import test from "node:test";

import { createTokenRenewer } from "./voice_token_renewal.js";

test("one renewal request refreshes both RTC and RTM tokens", async () => {
  const calls = [];
  const session = { session_id: "vs-1", rtc_token: "old-rtc", rtm_token: "old-rtm" };
  const renew = createTokenRenewer({
    session, participantUid: "1001",
    requestRenewal: async (sessionId, uid) => {
      calls.push(["api", sessionId, uid]);
      return { rtc_token: "new-rtc", rtm_token: "new-rtm", expires_at: "later" };
    },
    rtc: { renewToken: async (token) => calls.push(["rtc", token]) },
    rtm: { renewToken: async (token) => calls.push(["rtm", token]) },
  });
  await renew();
  assert.deepEqual(calls, [["api", "vs-1", "1001"], ["rtc", "new-rtc"], ["rtm", "new-rtm"]]);
  assert.equal(session.rtc_token, "new-rtc");
  assert.equal(session.rtm_token, "new-rtm");
});

test("simultaneous expiry events share one renewal", async () => {
  let requests = 0;
  let release;
  const wait = new Promise((resolve) => { release = resolve; });
  const renew = createTokenRenewer({
    session: { session_id: "vs-1" }, participantUid: "1001",
    requestRenewal: async () => { requests += 1; await wait; return { rtc_token: "r", rtm_token: "m" }; },
    rtc: { renewToken: async () => {} }, rtm: { renewToken: async () => {} },
  });
  const first = renew();
  const second = renew();
  release();
  await Promise.all([first, second]);
  assert.equal(requests, 1);
});
