/* Browser-only Agora transport. AEGIS reasoning remains on the Python API. */
import AgoraRTC from "agora-rtc-sdk-ng";
import AgoraRTM from "agora-rtm";
import { AgoraVoiceAI, AgoraVoiceAIEvents } from "agora-agent-client-toolkit";
import { createTranscriptRelay } from "./transcript_relay.js";
import { createTokenRenewer } from "./voice_token_renewal.js";
import { createConnectionManager } from "./connection_state.js";

const voiceButton = document.getElementById("voice-toggle");
const voiceState = document.getElementById("voice-state");
const clientUid = String(Math.floor(100000 + Math.random() * 899999));
let connection = null;

const setState = (text, enabled = true) => {
  voiceState.textContent = text;
  voiceButton.disabled = !enabled;
};

const headers = () => {
  const token = new URLSearchParams(location.search).get("token");
  return {
    "Content-Type": "application/json",
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
  };
};

async function api(url, options = {}) {
  const response = await fetch(url, { ...options, headers: headers() });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.error?.message || `Voice request failed (${response.status})`);
  return body;
}

/* Built per session, not once per page: the turn ids AEGIS deduplicates on
 * are scoped to the voice session, so the relay cannot be created before one
 * exists. See scopedTurnId in transcript_relay.js. */
const relayForSession = (sessionId) => createTranscriptRelay({
  participantUid: clientUid,
  sessionId,
  post: (event) => api("/api/transcript", {
    method: "POST",
    body: JSON.stringify(event),
  }),
});

function waitForAgent(rtc, agentUid) {
    return new Promise((resolve, reject) => {
      const timeout = setTimeout(() => {
        rtc.off("user-joined", onUserJoined);
        reject(new Error("The agent did not join the RTC channel in time."));
      }, 20000);
      const onUserJoined = (user) => {
        if (String(user.uid) !== String(agentUid)) return;
        clearTimeout(timeout);
        rtc.off("user-joined", onUserJoined);
        resolve();
      };
      rtc.on("user-joined", onUserJoined);
    });
}

async function start() {
  setState("starting voice…", false);
  const session = await api("/api/voice/sessions", {
    method: "POST",
    body: JSON.stringify({ participant_uid: clientUid }),
  });
    const rtc = AgoraRTC.createClient({ mode: "rtc", codec: "vp8" });
    const rtm = new AgoraRTM.RTM(session.app_id || "", clientUid);
    
    let isAgentConnected = false;
    const connectionManager = createConnectionManager({
      rtc,
      rtm,
      onStateChange: (state) => {
        if (state === "disconnected" || state === "failed") {
          setState(`voice ${state}`);
        } else if (state === "reconnecting") {
          setState(`voice reconnecting…`, false);
        } else if (state === "connected") {
          setState(isAgentConnected ? "voice connected" : "voice connected (waiting for agent)");
        } else {
          setState(`voice ${state}…`, false);
        }
      }
    });

    // App ID is public; return it explicitly rather than deriving it from any secret.
    if (!session.app_id) throw new Error("Voice session response did not include the Agora App ID.");
    try {
      await rtm.login({ token: session.rtm_token });
      await rtm.subscribe(session.channel);
      const ai = await AgoraVoiceAI.init({ rtcEngine: rtc, rtmConfig: { rtmEngine: rtm } });
      const transcriptToAegis = relayForSession(session.session_id);
      ai.on(AgoraVoiceAIEvents.TRANSCRIPT_UPDATED, (history) => {
        history.forEach((item) => transcriptToAegis(item).catch((error) => {
          console.warn("Transcript relay failed", error);
        }));
      });
      ai.on(AgoraVoiceAIEvents.AGENT_STATE_CHANGED, (_agentUid, event) => {
        isAgentConnected = (event.state === "connected");
        setState(`voice: ${event.state}`, true);
      });
      await rtc.join(session.app_id, session.channel, session.rtc_token, clientUid);
      const microphone = await AgoraRTC.createMicrophoneAudioTrack();
      await rtc.publish([microphone]);
      await ai.subscribeMessage(session.channel);
      await waitForAgent(rtc, session.agent_uid);
      connection = { session, rtc, rtm, ai, microphone, connectionManager };
    const renewTokens = createTokenRenewer({
      session: connection.session,
      participantUid: clientUid,
      requestRenewal: (sessionId, participantUid) => api(
        `/api/voice/sessions/${sessionId}/renew`,
        { method: "POST", body: JSON.stringify({ participant_uid: participantUid }) },
      ),
      rtc,
      rtm,
    });
    const renewOrLeave = () => renewTokens().catch(async (error) => {
      console.warn("Agora token renewal failed", error);
      try { await stop(); } catch (_) { /* best-effort safe cleanup */ }
      setState("voice token expired — rejoin required");
    });
    rtc.on("token-privilege-will-expire", renewOrLeave);
    rtm.addEventListener("tokenPrivilegeWillExpire", renewOrLeave);
    voiceButton.textContent = "Leave voice";
    setState("voice connected");
  } catch (error) {
    if (connectionManager) connectionManager.destroy();
    await rtc.leave().catch(() => {});
    await rtm.logout().catch(() => {});
    await api(`/api/voice/sessions/${session.session_id}`, {
      method: "DELETE", body: JSON.stringify({ participant_uid: clientUid }),
    }).catch(() => {});
    throw error;
  }
}

async function stop() {
  if (!connection) return;
  setState("leaving voice…", false);
  const { session, rtc, rtm, ai, microphone, connectionManager } = connection;
  if (connectionManager) {
    connectionManager.destroy();
  }
  microphone.close();
  ai.destroy();
  await rtc.leave();
  await rtm.logout();
  await api(`/api/voice/sessions/${session.session_id}`, {
    method: "DELETE", body: JSON.stringify({ participant_uid: clientUid }),
  });
  connection = null;
  voiceButton.textContent = "Join voice";
  setState("voice off");
}

voiceButton.addEventListener("click", async () => {
  try {
    if (connection) await stop(); else await start();
  } catch (error) {
    console.error("Agora voice error", error);
    connection = null;
    voiceButton.textContent = "Join voice";
    setState(error.message || "voice connection failed");
  }
});

window.addEventListener("beforeunload", () => { if (connection) stop(); });
