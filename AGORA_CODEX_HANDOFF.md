# AEGIS Agora Integration — Codex Handoff

## 1. Executive Status

- **Overall:** PARTIAL — server session scaffolding and browser transport source exist; live Agora operation has not been validated.
- **Current phase:** browser RTC/RTM session lifecycle and final-transcript bridge.
- **COMPLETE (unit/mock):** server configuration surface, server-only token minting, in-memory voice-session ownership, session-aware speech sink selection, protected voice-session routes, browser source/bundle, final-only/deduplicated transcript relay, and RTC/RTM token renewal.
- **PARTIAL:** browser reconnect and backend-restart recovery are not yet wired.
- **NOT STARTED:** a dedicated backend transcript-normalization adapter with contract tests based on a captured real Agora event.
- **BLOCKED:** real Agora project credentials/configuration are absent.
- **REQUIRES LIVE VALIDATION:** ConvoAI `/join`, RTC remote audio, RTM transcript delivery, microphone publishing, `INTERRUPT` speech, managed turn detection, token interoperability, and multi-speaker attribution.

## 2. Repository State

- Branch: `main`
- HEAD: `7bfd93eb16f6ebcf4168898dc0e290624e50bf1c`
- No checkpoint commit was created. The working tree includes unrelated/untracked user material: `.agents/`, `AGORA_INTEGRATION.md`, and `track-a/`; do not reset or commit them incidentally.
- Agora-related modified/added files are listed below. `frontend/node_modules/` is ignored; `frontend/package-lock.json` captures its reproducible dependencies.

## 3. Completed Implementation

### File
`backend/common/config.py`, `.env.example`, `requirements.txt`

### Change
Added server-only `AGORA_APP_CERTIFICATE`, `AGORA_TOKEN_TTL_SECONDS`, a token-issuance capability check, and `agora-agents>=2.7,<3.0`.

### Reason
The browser requires short-lived RTC/RTM credentials without receiving the App Certificate.

### Verification
Covered indirectly by the API fail-closed test; existing configuration tests pass.

### File
`backend/agora/tokens.py`

### Change
Added `issue_voice_tokens()`, returning a channel/UID-scoped AccessToken2 ConvoAI token for both RTC and RTM, plus expiry metadata.

### Reason
Agora's ConvoAI starter uses the combined credential for the browser RTC and RTM clients. The App Certificate remains only in the backend process.

### Verification
Implemented but not live-tested. It relies on the installed official `agora-agents` token helper.

### File
`backend/agora/sessions.py`

### Change
Added in-process idempotent session start/renew/get/stop lifecycle around the existing REST `AgoraClient`; added `SessionAwareSpeechSink` to use Agora only while a session is active and otherwise preserve `RecordingSink` behavior.

### Reason
This retains AEGIS as the source of every intervention while giving it an agent/session ID to speak through.

### Verification
`backend/tests/test_voice_sessions.py` tests duplicate start and owner-only/idempotent stop using a fake Agora client.

### File
`backend/pipeline/factory.py`

### Change
Creates `VoiceSessionManager` only when both REST credentials and App Certificate configuration are present. Runtime cleanup leaves managed sessions.

### Verification
Full backend suite passes.

### File
`backend/api/app.py`

### Change
Added token-protected session endpoints: start, get, renew, and stop. Start response contains only frontend-safe `app_id`, channel, agent/user identity, scoped tokens, and expiry. It does not serialize secrets.

### Verification
`VoiceSessionRouteTests` proves the route fails closed without configuration and remains bearer-token protected.

### File
`frontend/package.json`, `frontend/package-lock.json`, `frontend/voice.js`, `frontend/voice.bundle.js`, `frontend/index.html`

### Change
Added Agora RTC, RTM, and ConvoAI toolkit dependencies; opt-in Join/Leave voice UI; microphone capture/publish; RTM login/subscription; toolkit transcript listener; agent join wait; best-effort cleanup; and expiration handlers that renew both RTC and RTM credentials through the existing backend renewal API. `voice.bundle.js` was built successfully with esbuild (4.5 MB).

### Reason
The existing dashboard remains in place; voice is a separate transport layer.

### Verification
`npm run test:voice` passes 6 focused browser-unit tests and `npm run build:voice` succeeds when run from `frontend/`. No real browser/Agora session was run.

### File
`frontend/transcript_relay.js`, `frontend/transcript_relay.test.js`

### Change
Extracted the browser transcript bridge into a dependency-free module. It accepts only toolkit `END` (`1`) turns from the actual user or the documented local-user sentinel (`"0"`), maps sentinel identity to the authenticated session participant, preserves the Agora turn ID, normalizes seconds/milliseconds timestamps to UTC ISO, and suppresses delivered/in-flight duplicates. Failures clear the in-flight mark so a later toolkit history update can retry.

### Reason
Toolkit transcript updates contain complete history. Treating `IN_PROGRESS` (numeric `0`) as final would mutate AEGIS state with partial speech; repeatedly posting history would create needless ingest work despite backend deduplication.

### Verification
Four Node tests prove interim/agent exclusion, identity/turn-ID/timestamp preservation, one post per completed turn, and safe retry after a failed post.

### File
`frontend/voice_token_renewal.js`, `frontend/voice_token_renewal.test.js`

### Change
Added a coalescing renewal helper and connected RTC `token-privilege-will-expire` and RTM `tokenPrivilegeWillExpire` handlers. It posts to the existing server renewal route, renews both SDKs, and attempts a safe leave if renewal fails.

### Verification
Two Node tests prove both tokens refresh and concurrent expiry events create only one renewal request.

## 4. Partially Completed Work

### File
`frontend/voice.js`

### Current state
It subscribes to the toolkit's complete transcript history and relays items whose UID equals the local browser UID and whose status is not `in_progress` to existing `POST /api/transcript`.

### Remaining work
This logic now has explicit delivered/in-flight turn tracking and uses the local installed toolkit declaration (`TurnStatus.END = 1`, `IN_PROGRESS = 0`). It still needs a captured live event to prove the configured ConvoAI service uses the expected shape and timestamp behavior.

### File
`backend/agora/tokens.py`

### Current state
The code uses one combined ConvoAI token for RTC and RTM based on Agora's quickstart pattern.

### Remaining work
Live-test it on the chosen Agora project; if this project's RTM policy requires a distinct RTM token, change the server builder only and document the official evidence.

## 5. Remaining Implementation

1. Handle RTC/RTM disconnect/reconnect and backend-restart recovery without creating duplicate agents or state mutations.
2. Add API tests with a fake `VoiceSessionManager` for start/get/renew/stop response shape, including asserting App Certificate/Customer credentials never appear.
3. Run browser smoke tests with the server, then run a live Agora spike once credentials exist.

## 6. Actual Architecture

```text
Browser mic -> Agora RTC -> ConvoAI managed STT/RTM -> toolkit transcript history
  -> POST /api/transcript (final human turns only) -> existing AEGIS worker
  -> extraction -> state store -> deterministic risk engine -> governor
  -> SessionAwareSpeechSink -> existing Agora REST /speak -> ConvoAI TTS -> RTC audio
```

AEGIS still owns transcript interpretation, incident state, evidence, risk, governor decisions, human confirmation, and every outbound speech decision. Agora only supplies the agent/RTC/RTM/STT/TTS transport.

## 7. Configuration

### Required server variables for live voice

`AGORA_APP_ID`, `AGORA_APP_CERTIFICATE`, `AGORA_CUSTOMER_ID`, `AGORA_CUSTOMER_SECRET`.

### Frontend-safe values returned by API

App ID, ephemeral RTC/RTM token, channel, participant UID, agent UID, session ID, expiry. Never return App Certificate or REST Customer credentials.

### Optional variables

`AGORA_AGENT_UID` (defaults to `9000`), `AGORA_TOKEN_TTL_SECONDS` (defaults to `3600`), `AGORA_CHANNEL_NAME` (legacy/default; sessions generate a unique channel).

### Agora Console requirements

Create/configure a project with App Certificate token authentication, enable ConvoAI/RTM, and provision RESTful API Customer ID/Secret for the existing REST agent lifecycle client. Managed STT/LLM/TTS requires no separate provider keys for this path.

### Local dependencies

Python: `agora-agents`. Frontend: Agora RTC 4.24.3, RTM 2.2.4, client toolkit 1.2.0, esbuild. npm had to use `--legacy-peer-deps` because RTM declares an outdated strict RTC peer while the toolkit needs a newer RTC version.

## 8. Tests

### UNIT / MOCK

- `python -m pytest backend/tests/test_voice_sessions.py backend/tests/test_agora.py -q` — **33 passed**; session lifecycle and existing mock REST client/speech tests.
- `python -m pytest backend/tests/test_api.py backend/tests/test_voice_sessions.py -q` — **45 passed**; protected, fail-closed voice-route behavior.
- `python -m pytest backend/tests -q` — **372 passed**; existing AEGIS regression suite plus new tests. A pre-existing pytest cache permission warning remains.
- `npm run build:voice` in `frontend/` — succeeds; bundle generated.

### LIVE

Not performed: credentials/configuration are missing.

## 9. Known Problems

- No live credentials, agent, or project configuration: no live behavior may be claimed.
- Browser reconnection is not implemented.
- Session state is in-memory, so a backend restart loses the map and may orphan a live ConvoAI agent; safe cleanup/recovery design is still needed.
- Browser dependency metadata has a peer conflict resolved by npm legacy mode; verify in a real supported browser.
- `agora-agents` emits a Python 3.14/Pydantic compatibility warning when imported in this environment; token builder behavior needs live verification on the deployment Python version.

## 10. Important Decisions

- Preserve the existing `/api/transcript` and pipeline rather than creating an Agora-specific incident state path.
- Do not enable live Agora speech until a voice session is active; fallback recording behavior remains unchanged for demos/tests.
- Use server-generated unique channels and positive numeric browser UIDs.
- Treat the browser App ID/token/channel as safe transport values; keep all credential material server-only.

## 11. Exact Next Step

First add RTC/RTM connection-state handlers in `frontend/voice.js` that show a safe reconnecting/unavailable state without attempting to fabricate transcripts or create a second server agent; write unit tests around the state-transition helper.

## 12. Instructions to Antigravity

Read this handoff first, then inspect the repository yourself and verify every statement against source/tests. Do not redo working pieces; preserve AEGIS safety boundaries. Continue from **Exact Next Step**, run tests after each change, update this handoff, and never claim live Agora behavior without a real credentialed test.
