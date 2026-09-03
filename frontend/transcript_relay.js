/* Normalize Agora toolkit transcript history into AEGIS's existing ingress. */

// `TurnStatus` is a numeric enum in agora-agent-client-toolkit: END is 1.
// Keep the string form as a narrow compatibility allowance for serialized
// toolkit events; do not treat an unknown status as final.
const isCompleted = (status) => status === 1 || String(status).toUpperCase() === "END";

function timestampToIso(value) {
  if (typeof value === "number" && Number.isFinite(value)) {
    // Agora's normalized `_time` may be seconds or milliseconds. Preserve the
    // instant while emitting the ISO timestamp expected by TranscriptEvent.
    const milliseconds = Math.abs(value) < 100_000_000_000 ? value * 1000 : value;
    const timestamp = new Date(milliseconds);
    return Number.isNaN(timestamp.getTime()) ? null : timestamp.toISOString();
  }
  if (typeof value === "string") {
    const timestamp = new Date(value);
    return Number.isNaN(timestamp.getTime()) ? null : timestamp.toISOString();
  }
  return null;
}

export function normalizeFinalHumanTurn(item, participantUid) {
  if (!item || !participantUid) return null;

  // The toolkit hardcodes `status = 1` (END) for all user transcripts, even interim ones.
  // We must inspect the raw message metadata to determine if it is truly final.
  const isFinal = (item.metadata && typeof item.metadata.final === "boolean")
    ? item.metadata.final
    : isCompleted(item.status);

  if (!isFinal) return null;

  // The toolkit represents the local speaker as "0" in some transcript modes.
  // The authenticated voice-session UID is the identity AEGIS records.
  if (String(item.uid) !== String(participantUid) && String(item.uid) !== "0") return null;
  const turnId = String(item.turn_id || "").trim();
  const text = String(item.text || "").trim();
  const timestamp = timestampToIso(item._time);
  if (!turnId || !text || !timestamp) return null;
  return {
    uid: String(participantUid),
    turn_id: turnId,
    role: "human",
    text,
    final: true,
    timestamp,
  };
}

export function createTranscriptRelay({ participantUid, post }) {
  const delivered = new Set();
  const pending = new Set();

  return async (item) => {
    const event = normalizeFinalHumanTurn(item, participantUid);
    if (!event || delivered.has(event.turn_id) || pending.has(event.turn_id)) return false;
    pending.add(event.turn_id);
    try {
      await post(event);
      delivered.add(event.turn_id);
      return true;
    } finally {
      pending.delete(event.turn_id);
    }
  };
}
