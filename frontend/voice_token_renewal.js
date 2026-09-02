/* Token renewal is transport-only: it never changes AEGIS incident state. */

export function createTokenRenewer({ session, participantUid, requestRenewal, rtc, rtm }) {
  let inFlight = null;

  return async () => {
    if (inFlight) return inFlight;
    inFlight = (async () => {
      const refreshed = await requestRenewal(session.session_id, participantUid);
      await Promise.all([
        rtc.renewToken(refreshed.rtc_token),
        rtm.renewToken(refreshed.rtm_token),
      ]);
      Object.assign(session, refreshed);
      return refreshed;
    })();
    try {
      return await inFlight;
    } finally {
      inFlight = null;
    }
  };
}
