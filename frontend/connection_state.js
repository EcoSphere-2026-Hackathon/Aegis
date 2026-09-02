/* connection_state.js */
export function createConnectionManager({ rtc, rtm, onStateChange }) {
  let rtcState = "DISCONNECTED";
  let rtmState = "DISCONNECTED";

  const updateOverallState = () => {
    // Both must be connected to be considered "connected"
    if (rtcState === "CONNECTED" && rtmState === "CONNECTED") {
      onStateChange("connected");
    } else if (rtcState === "FAILED" || rtmState === "FAILED") {
      onStateChange("failed");
    } else if (rtcState === "RECONNECTING" || rtmState === "RECONNECTING") {
      onStateChange("reconnecting");
    } else if (rtcState === "CONNECTING" || rtmState === "CONNECTING") {
      onStateChange("connecting");
    } else if (rtcState === "DISCONNECTED" || rtmState === "DISCONNECTED" || rtcState === "DISCONNECTING" || rtmState === "DISCONNECTING") {
      onStateChange("disconnected");
    }
  };

  const handleRtcState = (curState, _revState, _reason) => {
    rtcState = curState;
    updateOverallState();
  };

  const handleRtmState = (event) => {
    rtmState = event.state;
    updateOverallState();
  };

  rtc.on("connection-state-change", handleRtcState);
  rtm.addEventListener("status", handleRtmState);

  return {
    destroy: () => {
      rtc.off("connection-state-change", handleRtcState);
      rtm.removeEventListener("status", handleRtmState);
    },
    getCurrentState: () => {
      // For initial testing or manual checks
      return { rtc: rtcState, rtm: rtmState };
    }
  };
}
