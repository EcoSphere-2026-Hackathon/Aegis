import test from "node:test";
import assert from "node:assert";
import { createConnectionManager } from "./connection_state.js";

// Mock Event Emitters
class MockRTC {
  constructor() { this.listeners = {}; }
  on(event, cb) { this.listeners[event] = cb; }
  off(event, cb) { if (this.listeners[event] === cb) delete this.listeners[event]; }
  emit(event, ...args) { if (this.listeners[event]) this.listeners[event](...args); }
}

class MockRTM {
  constructor() { this.listeners = {}; }
  addEventListener(event, cb) { this.listeners[event] = cb; }
  removeEventListener(event, cb) { if (this.listeners[event] === cb) delete this.listeners[event]; }
  emit(event, arg) { if (this.listeners[event]) this.listeners[event](arg); }
}

test("Connection Manager: overall state is connected only when both are connected", () => {
  const rtc = new MockRTC();
  const rtm = new MockRTM();
  let lastState = null;

  const manager = createConnectionManager({
    rtc,
    rtm,
    onStateChange: (state) => { lastState = state; }
  });

  // Initially disconnected
  assert.equal(lastState, null);

  // One connecting
  rtc.emit("connection-state-change", "CONNECTING", "DISCONNECTED", "");
  assert.equal(lastState, "connecting");

  // Both connecting
  rtm.emit("status", { state: "CONNECTING", reason: "" });
  assert.equal(lastState, "connecting");

  // One connected, one connecting
  rtc.emit("connection-state-change", "CONNECTED", "CONNECTING", "");
  assert.equal(lastState, "connecting");

  // Both connected
  rtm.emit("status", { state: "CONNECTED", reason: "" });
  assert.equal(lastState, "connected");

  // One reconnecting -> overall reconnecting
  rtc.emit("connection-state-change", "RECONNECTING", "CONNECTED", "");
  assert.equal(lastState, "reconnecting");

  // Both reconnecting
  rtm.emit("status", { state: "RECONNECTING", reason: "" });
  assert.equal(lastState, "reconnecting");

  // One failed -> overall failed
  rtm.emit("status", { state: "FAILED", reason: "" });
  assert.equal(lastState, "failed");
  
  // Cleanup
  manager.destroy();
  assert.equal(Object.keys(rtc.listeners).length, 0);
  assert.equal(Object.keys(rtm.listeners).length, 0);
});
