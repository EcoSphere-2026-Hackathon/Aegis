# AEGIS — FINAL SINGLE SOURCE OF TRUTH

**Status as of:** Sep 1, 2026 — Round 3 (Development Sprint), hard submission deadline 4 Sep 2026, 11:59 PM IST.
**Nothing has been implemented yet.** This document consolidates all strategy, architecture, and verification work completed before any code was written.

---

## 1. PROJECT IDENTITY

- **Project name:** AEGIS
- **Hackathon:** EchoSphere: Agora Conversational AI Hackathon, organized by KNOTiC
- **Team:** AI Slayers — Ayush Kumar (Team Lead), Ansh Darji (Team Member)
- **Track:** Applied AI / AI Engineering
- **Problem statement:** PS4 — Voice AI Incident Commander (locked; hackathon rules forbid inventing a new problem statement)
- **One-line description:** A voice-native AI participant that joins a live incident call, builds a trustworthy shared operational state from the conversation, and intervenes via real-time voice to demand human confirmation before any consequential action is treated as authorized.
- **Core problem:** During a live P1 incident, teams rarely fail from missing information — they fail because a hedged guess quietly becomes treated as fact, an explicit decision is forgotten under pressure, and a destructive action gets verbally approved faster than anyone can check its consequences.
- **Core solution:** AEGIS continuously extracts structured claims (facts, hypotheses, decisions, proposed actions) from live incident-room speech — and, per the multimodal extension (§29), from typed text and submitted screenshots — evaluates every proposed action against a deterministic risk engine (staleness, decision-reversal, topology blast-radius, telemetry/visual evidence grounding), and barges in over live audio to require explicit human confirmation before treating any action as authorized.
- **Target users:** SRE / incident-response teams running live P1/P2 incidents.
- **Core value proposition:** Fewer irreversible actions taken on unconfirmed guesses; fewer repeated mistakes; less epistemic drift under pressure; a visible, auditable record of what was decided and why.

---

## 2. FINAL PRODUCT DEFINITION

**What the user experiences:** Engineers speak naturally on a live Agora voice call, as they would in any incident bridge. AEGIS listens silently by default. When a claim needs correction against reality, or a proposed action conflicts with prior decisions or system topology, AEGIS speaks up — states the exact rule or evidence gap violated — and asks for explicit confirmation before the action is treated as authorized. On request ("AEGIS, status?"), it gives a spoken summary of open hypotheses, held decisions, and unresolved risk. At the end, it produces a final incident summary from the state it built.

**What the system does:** Extraction → structured state → deterministic risk evaluation → rate-limited intervention → human confirmation → state update. A closed loop, not a one-shot pattern-match.

**What makes it different:** It is not a notetaker and not a chatbot — no one talks *to* it by default. It is an unsolicited-intervention system that grounds claims against both conversational consistency (staleness, decision-reversal, topology) and reality (mocked telemetry), which distinguishes it from a purely conversational-consistency checker.

**What it explicitly does NOT do:**
- Does not execute, block, or override any action, under any circumstance.
- Does not present uncertain AI-generated information as confirmed fact.
- Does not provide authoritative legal/financial/emergency instructions.
- Does not integrate with any real third-party system (Jira/Slack/PagerDuty) — all "tools" beyond the mocked telemetry endpoint are demo-scoped and non-destructive.

**Core product workflow:** Conversation (+ optional typed text / submitted screenshots, §29) → LLM structured extraction (text mode + vision mode) → Incident State Store update → deterministic risk evaluation (staleness + decision-reversal + topology + telemetry/visual evidence) → Intervention Governor decision (SILENT/SUGGEST/ASK/WARN) → Agora `speak` broadcast if warranted → human spoken confirm/deny/hold → State Store updated with the human's decision → loop continues.

---

## 3. FINAL STRATEGIC THESIS

**Underlying problem:** Incident response fails less from missing information than from three specific epistemic failures under pressure: (1) a hedged guess is treated as confirmed fact, (2) an explicit decision is forgotten and re-litigated or reversed without new evidence, (3) a destructive action is verbally approved faster than anyone — human or tool — can catch its consequences.

**Why existing approaches are insufficient:** A typed warning (Slack bot, dashboard) arrives after a command may already be running. The entire premise of intervening *before* execution requires sub-second, multi-party, live voice — a text UI cannot deliver a warning fast enough to matter in the window between "someone proposes an action" and "someone executes it."

**Core insight:** Separate understanding (LLM) from deciding (deterministic code) from authorizing (human). The LLM only converts messy speech into typed claims — it never makes a safety/risk call. A Python risk engine evaluates every proposed action against explicit state and topology. A human explicitly authorizes anything consequential. This separation directly answers the hackathon's own safety restriction ("must not present uncertain AI-generated information as confirmed fact") and pre-empts the most likely judge question ("how do you prevent a hallucinated safety decision?").

**Why the approach is innovative:** Most competing teams in this track will build a notetaker or a Q&A assistant. AEGIS is an *unsolicited-intervention* system — it must decide not just what to say, but whether to interrupt a live human conversation at all, and its intelligence is largely invisible until the moment it correctly does so. Reality-grounding (telemetry) adds a second, materially different reasoning capability beyond conversational consistency alone.

**Real-world value:** SRE/incident teams are a real, articulable buyer with a real, expensive failure mode (bad rollbacks, wasted MTTR chasing stale hypotheses).

---

## 4. FINAL ARCHITECTURE — FROZEN

```
Agora Voice (multi-party RTC, remote_rtc_uids: ["*"])
   → RTM transcript delivery (client relays to backend)
   → LLM Structured Extraction (own backend service — NOT Agora's built-in llm turn slot)
   → Incident State Store (facts, hypotheses w/ staleness, decision ledger, ownership, timeline, proposed actions, evidence)
   → risk_engine.evaluate(proposed_action, state, topology, evidence[])   [ONE function, three-plus internal checks]
       - staleness check
       - decision-reversal check
       - topology blast-radius check (networkx graph traversal)
       - telemetry/visual evidence contradiction check (Phase 2+ input, same function — generalized by the multimodal extension, §29, from telemetry-only to telemetry+visual evidence)
   → Intervention Governor (SILENT → SUGGEST → ASK → WARN, rate-limited ~1 per 45s)
   → Agora speak (TTS broadcast, priority: INTERRUPT) if WARN/ASK
   → Human spoken confirm/deny/hold (captured via the same ASR→extraction pipeline)
   → State Store updated with the human's decision; proposed action marked resolved
```

**FROZEN decisions in this diagram** (do not reopen — see §25 for the full list):
- Architecture B ("Grounded Commander") is the chosen design.
- The three originally-pitched "engines" (Justification Audit, Decision-Reversal, Assumption-Drift) are collapsed into **one** `risk_engine.evaluate()` function over **one** shared state store.
- Telemetry grounding is a Phase 2 upgrade — 4 fixed mocked metrics, built only after the core reactive pipeline works end-to-end.
- AEGIS never executes, blocks, or overrides a human action, under any circumstance.

---

## 5. AEGIS FINAL ARCHITECTURE — COMPONENT PIPELINE

1. **Agora Conversational AI Engine (RTC ingestion)** — only Agora-native way to get an AI participant into a multi-person live voice room with per-speaker attribution and managed ASR.
2. **Structured LLM Extraction** — separate backend service converting one "final" transcript utterance (+ recent context) into zero or more typed claims: `fact | hypothesis | decision | proposed_action`, tagged with speaker UID and timestamp.
3. **Incident State Store** — in-memory or SQLite (for demo crash-safety); single source of truth for facts, hypotheses (staleness/reinforcement tracked), decision ledger, ownership tags, timeline, proposed actions.
4. **Topology / Risk Evaluation** — `risk_engine.evaluate()`, using a real ~8–12 node `networkx` dependency graph with typed edges (`depends_on`, `reads_schema`, `compatible_with`) and BFS/DFS traversal for blast-radius — this is the team's primary technical-depth proof point, replacing the original PPT's flat `topology.json` keyword-matching.
5. **Intervention Governor** — explicit state machine `SILENT → SUGGEST → ASK → WARN`, hard rate-limited to ≤1 spoken intervention per ~45s.
6. **Agora TTS Intervention** — `speak` REST endpoint, `priority: INTERRUPT`, called against the live `agent_id` when the Governor outputs WARN/ASK.
7. **Human Confirmation** — spoken reply captured through the same ASR→extraction pipeline, classified as `confirmation`/`override`/`hold`.
8. **State Update** — human's decision appended to the Decision Ledger; proposed action marked resolved; listening continues.
9. **Telemetry Grounding (Phase 2 only)** — 4 fixed mock metrics feeding the same `risk_engine.evaluate()` as an additional input producer, not a new pipeline.
10. **Multimodal Evidence Ingestion (Phase 2 only, per the multimodal extension, §29)** — a minimal side-channel (outside Agora's transport) accepting typed text or an uploaded screenshot from a participant during the incident. Text is wrapped into the existing transcript-event shape and fed into the extraction service's existing text mode. Screenshots are fed into a new vision-mode code path in the same extraction service.
11. **Vision-mode extraction (Phase 2 only, per §29)** — a code path within the same Structured LLM Extraction service (item 2), not a separate system, producing `Evidence` objects that feed `risk_engine.evaluate()` alongside telemetry, generalizing item 9 above from "telemetry only" to "telemetry + visual evidence."

---

## 6. COMPONENT SPECIFICATIONS

| Component | Purpose | Inputs | Outputs | State owned | Must NOT do |
|---|---|---|---|---|---|
| Agora RTC/RTM layer | Multi-party audio ingestion + transcript/event delivery + TTS delivery | Human audio, `speak` calls | Transcript events, `AGENT_METRICS`, audio | None | Make risk decisions; execute actions |
| LLM Extraction service | Convert transcript text → typed claims | Final transcript utterance + recent context | `{type, text, speaker_uid, timestamp}` claims | None | Decide risk/safety; call itself "confirmed" |
| Incident State Store | Single source of truth | Extracted claims, human decisions | Current state snapshot | Facts, hypotheses, decisions, ownership, timeline, proposed actions | Execute or authorize anything |
| `risk_engine.evaluate()` | Deterministic safety/risk decision | Proposed action + state + topology + evidence (telemetry/visual, Phase 2+, §29) | `{risk_tier, reasons[]}` | None (pure function over State Store) | Use an LLM for the decision itself |
| Intervention Governor | Decide whether/how to speak | Risk verdict | `SILENT/SUGGEST/ASK/WARN` | Rate-limit timer | Bypass the 45s rate limit |
| Agora `speak` call | Deliver intervention audibly | Governor output text | Broadcast TTS | None | Execute/block the underlying action |
| Human confirmation capture | Capture the human's authorization | Human speech | `confirmation/override/hold` claim | None | Infer authorization without an explicit human utterance |
| Multimodal Evidence Ingestion (§29) | Accept text/screenshot input outside Agora's transport | Typed text, image upload | Transcript-event-shaped object (text) or `Evidence` object (image) | None | Bypass extraction/validation; auto-authorize anything |

---

## 7. AI ARCHITECTURE

- **LLM** = understands only. Structured extraction of typed claims from messy speech — and, in its vision-mode code path (§29), structured extraction of named-metric readings from a submitted screenshot. Never makes a risk/safety decision, in either mode.
- **Incident State** = remembers. Facts, hypotheses (w/ staleness), decision ledger, ownership, timeline.
- **Deterministic engine** = evaluates. Python logic against explicit topology + state; not model-guessed.
- **Tools** = execute only bounded, reversible actions (read-only telemetry query, a summary post) — never anything destructive.
- **Human** = authorizes anything consequential via explicit confirm/deny.

**Preserved boundary (state this near-verbatim to judges):**
`AI interpretation ≠ deterministic authorization ≠ human authorization ≠ execution`

No structured-output schema, prompt template, or extraction validation logic has been finalized yet — that is implementation work, not yet done.

---

## 8. STATE MODEL

| Entity | Key fields (agreed conceptually; exact schema NOT yet finalized) | Lifecycle |
|---|---|---|
| Fact | text, speaker_uid, timestamp | Stated → stays in state (no staleness) |
| Hypothesis | text, speaker_uid, timestamp, staleness/reinforcement count | Stated → reinforced, or determined contradicted by the risk-evaluation logic (a pure-function return value applied by the State Store — the engine itself never writes to state) |
| Decision | text, speaker_uid, timestamp | Stated → logged in Decision Ledger → checked for reversal on new proposed actions |
| Proposed Action | target, speaker_uid, timestamp | Stated → evaluated by risk engine → resolved (confirmed/declined/held) by human |
| Ownership tag | free metadata captured by extraction | Attached to claims as they're extracted |
| Timeline | ordered claims across the incident | Continuously appended |
| Evidence (§29, multimodal extension) | evidence_id, source_type (telemetry/visual), metric_name, value, unit, extraction_certainty, source, uploader_uid, timestamp, target_ref, raw_reference | Submitted/fetched → available to `risk_engine.evaluate()` → referenced in `RiskVerdict.reasons` when it drives a verdict. Deliberately NOT collapsed into Fact or Hypothesis — it is an observation about reality, not a human assertion. |

No persistence schema, staleness formula, or conflict-resolution rule has been finalized in code. **NEEDS DESIGN** during implementation, within the frozen conceptual model above.

---

## 9. DATA CONTRACTS

| Contract | Fields (as documented — schema not yet implemented) | Producer | Consumer | Status |
|---|---|---|---|---|
| Transcript event | `uid`, `turn_id`, `role`, `text`, `final` | Agora RTM | Backend extraction pipeline | VERIFIED (schema, per local Agora Skill) |
| `speak` request | `text` (max 512 bytes), `priority` (`INTERRUPT`/`APPEND`/`IGNORE`), `interruptable` (bool) | Backend Governor | Agora `speak` endpoint | VERIFIED (schema); `IGNORE` value corroborated only by local skill, not independently confirmed against official docs |
| `AGENT_METRICS` event | RTM event code `111`; ASR/LLM/TTS per-stage latency | Agora Conversational AI Engine | Backend/latency logging | VERIFIED (config path); exact payload shape NEEDS LIVE TESTING |
| Extracted claim | `type` (fact/hypothesis/decision/proposed_action), `text`, `speaker_uid`, `timestamp`, optional `source_modality` (`voice`/`text`, §29) | LLM extraction service | Incident State Store | NOT YET IMPLEMENTED — conceptually agreed, no JSON schema written |
| Extracted evidence (§29, multimodal extension) | `evidence_id`, `source_type` (telemetry/visual), `metric_name`, `value`, `unit`, `extraction_certainty` (high/low — categorical, not probabilistic), `source`, `uploader_uid`, `timestamp`, `target_ref`, `raw_reference` | LLM extraction service (vision mode) or mock telemetry endpoint | Incident State Store → `risk_engine.evaluate()` | NOT YET IMPLEMENTED — see §29 |
| Risk verdict | `risk_tier`, `reasons[]` | `risk_engine.evaluate()` | Intervention Governor | NOT YET IMPLEMENTED — conceptually agreed |

---

## 10. AGORA TECHNICAL SETUP

- **Product:** Agora Conversational AI Engine. VERIFIED as the correct current product.
- **Multi-party subscription:** `remote_rtc_uids: ["*"]` on the Start-agent (`/join`) endpoint. VERIFIED (schema, both official docs and local skill agree). Live correctness of two-simultaneous-human attribution: NEEDS TESTING.
- **Transcript mechanism:** RTM (Signaling), not a server-side webhook. Requires `advanced_features.enable_rtm: true` AND `parameters.data_channel: "rtm"` in the join payload (local-skill detail, more specific than general docs). Data Stream Mode exists as an alternative but is explicitly documented as not scaling past single-user — do not use it. VERIFIED.
- **UID attribution:** Transcript payload carries `uid`, `turn_id`, `role`, `text`, `final`. VERIFIED (schema). Reliability under real overlapping multi-party speech: NEEDS TESTING — highest-priority open risk.
- **`speak` (intervention):** `POST /v2/projects/{appid}/agents/{agentId}/speak`, payload `{text, priority, interruptable}`. `priority: INTERRUPT` is documented to immediately interrupt the agent's own current interaction to deliver the message. VERIFIED (documented contract). What happens acoustically when a human is actively speaking at that moment: NEEDS TESTING — this is the core barge-in premise and is genuinely unresolved.
- **`interrupt_mode`:** Controls how the *agent* reacts when a human's voice interrupts *its own* speech — values `interrupt`/`append`/`ignore`/`adaptive`/`keywords`. This is a different lever from silence-by-default (below). VERIFIED (schema).
- **Silence-by-default candidate mechanism:** `turn_detection.config.start_of_speech.mode: "manual"` disables automatic VAD-triggered agent turns (local-skill finding, not corroborated in official-docs search). This is offered as a **candidate alternative** to the original handoff plan of bypassing the agent's built-in `llm` slot entirely. **Which mechanism AEGIS will actually use is OPEN/UNRESOLVED** pending spike results — see §25.
- **Metrics/latency:** Enable via `parameters.enable_metrics: true`; delivered as RTM event code `111` with per-stage ASR/LLM/TTS latency. VERIFIED (config path, local-skill detail); payload shape NEEDS TESTING.
- **Authentication:** Raw REST calls (`/join`, `/speak`) require Basic Auth via **Customer ID + Customer Secret** (Agora Console → Developer Toolkit → RESTful API), Base64-encoded — NOT App ID + App Certificate. VERIFIED via official docs. **Whether this Customer ID/Secret pair actually exists yet in the Console has not been confirmed** — Antigravity's environment report only surfaced the App ID. **NEEDS CHECKING** before wiring `/join`/`/speak`.
- **SDK-first path:** `agora-agents` Python/TS/Go SDK is the currently-documented recommended integration path over hand-rolled REST. VERIFIED.

---

## 11. CURRENT AGORA ENVIRONMENT (per Antigravity, Sep 1 2026)

- **Agora CLI:** Installed and authenticated at `C:\Users\ANSH DARJI\AppData\Local\Programs\Agora\bin\agora.exe`.
- **Agora project:** "Default Project", ID `Xo4FYDFSa`, App ID `1dbe4a73adb1480086fedd84493f92a7`, region `global`.
- **Features confirmed enabled:** `rtc` (included), `rtm` (enabled), `convoai` (enabled). Token capability enabled. `agora project doctor --feature convoai` reports the project ready for ConvoAI development.
- **ASR / TTS / LLM vendor configuration:** Cannot be determined — no application code or `.env` exists in the workspace yet (clean project directory). NEEDS DECIDING before implementation, not blocking the spike.
- **Agora Skill location:** `C:\Users\ANSH DARJI\Documents\EchoSphere\.agents\skills\agora\` — `SKILL.md`, `references/conversational-ai/README.md`, `architecture.md`, `agent-toolkit.md` inspected.
- **Agora MCP:** Configured and accessible in Antigravity at `https://mcp.agora.io`, exposing `algolia_search_index_docs_portal_en` and `algolia_search_index_agora_api_ref` — accessible only from the local Antigravity environment, NOT from this chat session (confirmed via `search_mcp_registry` returning no Agora connector here).
- **Customer ID / Customer Secret:** Existence in Console NOT confirmed by the environment inspection performed so far.

*(No secrets, tokens, or certificates are recorded in this document, per instruction.)*

---

## 12. EXTERNAL DEPENDENCIES

| Dependency | Purpose | Where used | Verification status |
|---|---|---|---|
| Agora Conversational AI Engine | RTC ingestion, RTM transcript/metrics, TTS intervention | Core voice layer | VERIFIED available/enabled on project |
| `networkx` (Python) | Topology graph + BFS/DFS blast-radius traversal | `risk_engine.evaluate()` | Not yet installed/tested — planning-only decision |
| LLM provider (unspecified) | Structured extraction from transcript text (+ vision-mode extraction from screenshots, §29) | Extraction service | NOT YET DECIDED — open implementation choice. **NEEDS CONFIRMING once chosen:** whether the provider supports multimodal/vision input — if not, this is the one new dependency the multimodal extension could introduce (a vision-capable provider/model); flag rather than silently substitute. |
| SQLite (or in-memory) | Incident State Store persistence for demo crash-safety | State Store | Decision made in principle; not yet implemented |
| `mock_telemetry.py` (local mock endpoint) | Serves 4 fixed metrics (`pool_utilization`, `error_rate`, `p99_latency`, `schema_version`) | Telemetry grounding, Phase 2 only | Not yet built — deliberately deferred |

No real third-party integrations (Jira/Slack/PagerDuty) are planned — explicitly excluded, see §26.

---

## 13. ENVIRONMENT & CONFIGURATION

**Required (values not recorded here):**
- `AGORA_APP_ID` (known, non-secret — see §11)
- Agora Customer ID + Customer Secret (for Basic Auth on `/join`, `/speak`) — existence unconfirmed
- LLM provider API key (provider not yet chosen)
- Local dev requirements: Python (for `risk_engine`, `mock_telemetry.py`), Node/TS (for Agora client SDKs per official quickstarts)

**Deployment assumptions:** Fully local/demo-scoped for the hackathon — no production deployment target has been discussed or is in scope.

---

## 14. FINAL USER WORKFLOW

**Incident begins** → engineers join the Agora channel as distinct RTC UIDs; AEGIS agent joins with `remote_rtc_uids: ["*"]`.
**People speak** → audio flows through Agora RTC; ASR produces final transcripts delivered over RTM with `uid` attribution.
**AEGIS observes/interprets** → backend extraction service converts each final transcript into zero or more typed claims.
**State changes** → Incident State Store is updated: new facts recorded, hypotheses added/reinforced/staled, decisions logged, proposed actions queued.
**Risk evaluation** → any proposed action is run through `risk_engine.evaluate()` against staleness, decision-reversal, topology, and (Phase 2) telemetry.
**Intervention** → if the Governor outputs ASK/WARN and the rate limit allows, AEGIS calls `speak` with `priority: INTERRUPT`, stating the specific violated rule/evidence gap.
**Human confirmation** → the human's spoken reply is captured through the same pipeline and classified as confirm/deny/hold.
**Final incident state** → Decision Ledger updated; on request or at close, AEGIS produces a spoken and/or on-screen summary from the accumulated state.

---

## 15. CRITICAL REAL-TIME BEHAVIOR

**Latency budget (ESTIMATE, not yet measured):**

| Stage | Estimate |
|---|---|
| ASR final-result | ~200–500ms (Agora-managed) |
| Extraction LLM call | 300–800ms |
| State write + risk eval | <30ms |
| Governor decision | <5ms |
| `speak()` → audible TTS | ~400–800ms |
| **Total: utterance → intervention audible** | **~1–2s (ESTIMATE — the spike's purpose is to produce a real number, via `AGENT_METRICS` plus app-side timestamps)** |

**Verified vs. needs benchmarking:** The *mechanism* for measuring latency (Agora's `AGENT_METRICS` event, event code `111`, giving per-stage ASR/LLM/TTS numbers, plus app-side timestamp-at-`speak`-call vs. timestamp-at-audible-TTS) is VERIFIED. The actual numbers are NEEDS TESTING.

**Concurrency/attribution/interruption:** All NEEDS TESTING — see §24.

---

## 16. SAFETY MODEL

- AEGIS **never** executes, blocks, or overrides a human action, under any circumstance — non-negotiable, explicitly called out as something not to compromise even under time pressure.
- All safety/risk decisions are made by deterministic Python logic (`risk_engine.evaluate()`), never by the LLM.
- Any consequential action requires explicit human confirm/deny/hold before being treated as authorized.
- Interventions are hard rate-limited (≤1 per ~45s) to prevent alarm fatigue and protect demo stability.
- Tools are restricted to bounded, reversible actions only (read-only telemetry query, summary post) — nothing destructive is ever wired to a tool call.
- Ambiguous or absent human confirmation is **not** treated as authorization (fail-safe: no response = no authorization) — this is the intended behavior; exact timeout/ambiguity handling logic is **NOT YET DESIGNED**.

---

## 17. TOOLS & ACTIONS

| Tool | Purpose | Input | Output | Side effects | Confirmation required | Demo relevance |
|---|---|---|---|---|---|---|
| Mock telemetry query | Ground a claim against reality | Metric name | Value from `mock_telemetry.py` (4 fixed metrics) | None (read-only) | No (informational input to risk engine) | Beat 1 of golden demo |
| Final summary post | Produce closing artifact | Current state | Text/on-screen summary | None | No | Closing demo beat |
| Multimodal evidence ingestion (§29) | Ground a claim against visually-submitted reality | Typed text or an uploaded screenshot | `Evidence` object (visual) or transcript-event-shaped text object | None (read-only observation, same as telemetry query) | No for ingestion itself (only the resulting proposed action, if any, requires confirmation) | Optional/IF-TIME candidate beat only, §29 |

No other tools are planned. No real third-party tool integration exists or is planned.

---

## 18. TESTING & EVALUATION

**Immediate (spike-level):** See §22/§24 — validate Agora plumbing only, no AEGIS intelligence code involved.

**Planned build-order testing (post-spike, per frozen decision in §25):**
1. Topology/risk engine built and unit-tested standalone, no voice component.
2. Incident State Store tested independently.
3. Extractor tested offline against a written transcript (no live audio).
4. Governor tested against synthetic risk verdicts.
5. Text-only pipeline wired end-to-end.
6. THEN live Agora integration.
7. Full live rehearsal of the golden demo script.

**Capability → Test → Evidence** (only entries actually agreed so far):

| Capability | Test | Evidence |
|---|---|---|
| Multi-speaker UID attribution | Two humans speak in the same channel | Transcript log with correct, distinct UIDs |
| Barge-in delivery | `speak(priority=INTERRUPT)` while human speaking | Observed audio behavior, recorded |
| Silence-by-default | Two humans converse with no `/speak` call | Agent produces no unsolicited output |
| Latency | Multiple `speak` calls, repeated | `AGENT_METRICS` + app timestamps, distribution not single sample |
| Topology blast-radius | Rollback proposal vs. known-incompatible schema | Risk verdict marks HIGH with correct reason | *(not yet run — implementation not started)* |

No AI evaluation set, hallucination-rate measurement, or extraction-accuracy benchmark has been defined yet. **OPEN / UNRESOLVED.**

---

## 19. HACKATHON JUDGING ALIGNMENT

| Judging Requirement | Our Capability | Technical Evidence | Demo Evidence |
|---|---|---|---|
| Innovation & Creativity | Unsolicited-intervention Incident Commander, not a notetaker/chatbot | Architecture doc (§4–5) | Two-beat golden demo (§20) |
| Use of Agora Conversational AI | RTC multi-party ingestion + RTM transcript/metrics + `speak` barge-in as structurally necessary, not decorative | §10 verification table | Live spike results |
| Technical Implementation | Real `networkx` topology traversal, deterministic risk engine, single state store discipline | §5–9 | Golden demo beat 2 (compound catch) |
| Quality of Voice/Conversational Experience | Barge-in over humans (harder than default agent-interrupted-by-user pattern) | §10, §15 | Live demo interruption behavior |
| Real-world Impact & Usefulness | SRE/incident teams, real expensive failure mode | §3 | — |
| Product Readiness & Scalability | Honest scope discipline (mocked topology/telemetry explicitly disclosed as reliability decision, not hidden) | §12, §26 | — |
| Live Demo & Presentation | Rehearsed golden path + fallback recorded clip (recommended, not yet built) | §20 | — |

---

## 20. FINAL DEMO

**Setup:** Two teammates on separate devices, joined as distinct UIDs (e.g. `1001`, `1002`). AEGIS agent joined, subscribed to both.

| Beat | Speaker | Line | Proves |
|---|---|---|---|
| 1 | Eng A | "Payments are throwing 500s, seeing timeouts." | Extracted as `fact`; Governor stays SILENT on ordinary chatter |
| 2 | Eng B | "Pool utilization looks fine, like 40%." | `hypothesis` + metric claim; telemetry grounding fires (mocked shows 91%) → HIGH |
| 3 | **AEGIS** | "Hold — telemetry shows pool utilization at 91%, not 40%. Want to re-check before ruling it out?" | **KILLER DEMO MOMENT #1** — grounds a claim against reality, not just conversational consistency |
| 4 | Eng A | "Okay, fine, it is the pool then. Let's rollback Core to the last version." | `proposed_action`; prior hypothesis flagged stale |
| 5 | (system) | — | Graph traversal: `core-db` rollback → `payment-api`, `auth-service` incompatible with target schema v2.3 |
| 6 | **AEGIS** | "Hold — two issues. That pool root-cause still isn't confirmed. And rolling back Core will break payment-api and auth-service — they're on schema v17, incompatible with v2.3." | **KILLER DEMO MOMENT #2** — compound stale-hypothesis + blast-radius catch, enriched by beat 3's correction |
| 7 | Eng B | "Hold — don't rollback, let's check the pool metrics properly first." | Human-authorization boundary respected; decision logged |
| 8 | Eng A | "AEGIS, status?" | On-demand spoken summary |
| 9 | (end) | — | Final incident summary shown as closing artifact |

**The single KILLER DEMO MOMENT** that best communicates the core innovation is **beat 3** (telemetry contradiction) immediately compounding into **beat 6** (stale hypothesis + blast-radius) in the same short exchange — two independently-caught reasoning failures, not one repeated trick.

**Fallback:** A recorded backup clip in case live ASR/interruption fails during judged demo — **recommended but not yet built**.

**Optional multimodal addition (§29, IF TIME, requires explicit team sign-off before inclusion in the judged run — does NOT alter the two locked beats above):** a candidate additional beat where Eng B uploads a dashboard screenshot showing 91% pool utilization instead of (or alongside) the mocked telemetry value, and AEGIS's intervention is driven by `Evidence.source_type: visual` rather than `telemetry`. This reuses the exact same intervention/confirmation mechanism as beat 3 — see §29 for the full scenario and the reasoning for keeping it optional rather than locked.

---

## 21. IMPLEMENTATION PRIORITIES

**MUST BUILD**
1. Multi-speaker ASR → LLM structured extraction — build and stress-test first, highest-risk component.
2. Incident State Store with staleness timestamps and decision ledger.
3. Real topology graph + BFS traversal for blast-radius/compatibility checks.
4. Intervention Governor wired to `speak`.
5. One rehearsed golden-path scenario, fully deterministic, end-to-end.

**SHOULD BUILD**
6. Telemetry-grounding tool call (4 fixed metrics) → reality-vs-claim conflict detection.
7. Conflict detection over a fixed conversational claim schema.
8. On-demand spoken status summary.
9. Live timeline visualization.
10. Multimodal evidence ingestion (text + screenshot side-channel) and vision-mode extraction, generalizing `risk_engine.evaluate()`'s telemetry input to telemetry+visual evidence (§29).

**IF TIME**
11. Proactive nudges (rate-limited).
12. Auto-generated final incident summary.
13. Task-ownership UI treatment.
14. Evidence UI treatment (thumbnail + extracted reading alongside telemetry) and the optional multimodal demo beat (§29, §20) — sign-off-gated.

**DO NOT BUILD**
- Any real Jira/Slack/PagerDuty integration.
- Speaker diarization ML (per-speaker attribution expected from Agora's per-UID transcript tagging — this specific claim is itself part of what the spike must verify).
- Multiple named "engines" as separate services — one `risk_engine.evaluate()` function only.
- Open-domain contradiction detection beyond the fixed claim-slot schema.
- Bayesian/probabilistic confidence modeling (this includes visual evidence: `extraction_certainty` is a categorical high/low flag, not a probability score — §29).
- Multi-agent personas/voices.
- Any path where AEGIS executes a proposed action itself.
- Open-domain image understanding beyond named-metric dashboard-style readings (§29) — no general-purpose computer vision, no separate vision "engine" or agent.

---

## 22. IMPLEMENTATION ORDER

1. **Agora real-time plumbing spike** (current immediate task, not yet started) — proves the 4 target verifications below, zero AEGIS intelligence code.
2. Topology/risk engine built and unit-tested standalone (pure Python, no voice dependency — can run in parallel with or even before the spike).
3. Incident State Store built and tested independently.
4. Extractor tested offline against a written transcript.
5. Governor tested against synthetic verdicts.
6. Text-only pipeline wired end-to-end.
7. Live Agora integration.
8. Full live rehearsal of the golden demo.

**Why this order:** risk + dependency + validation value — the two hardest live-only unknowns (UID attribution, barge-in behavior) could force an architecture change (e.g. falling back to the LLM-slot-bypass mechanism instead of `start_of_speech: manual`), so no further hours should go into live intelligence-layer integration until they're empirically resolved. The reasoning components are pure Python and independent of live voice, so they can and should proceed in parallel.

---

## 23. KNOWN RISKS

| Risk | Impact | Probability | Detection | Mitigation | Fallback | Status |
|---|---|---|---|---|---|---|
| Multi-speaker UID misattribution | Extraction pipeline trusts wrong speaker for a claim | Unknown | Spike step: two humans speak, inspect logs | None yet — spike is the mitigation | Manual UID tagging workaround if native attribution fails | OPEN |
| `speak(INTERRUPT)` doesn't reliably cut through live human speech | Core barge-in premise weakened; must reframe demo claim | Unknown | Spike step 6 | Use `interruptable: false` via custom LLM integration if `speak` proves too fragile (docs-referenced, not yet explored in depth) | Reframe pitch language to "delivers immediately, can in principle be talked over" | OPEN |
| Agent talks over human-to-human conversation despite silence attempt | Breaks the "silent by default" premise entirely | Unknown | Spike step 4 | Fall back to bypassing the agent's `llm` slot entirely (original handoff plan) if `start_of_speech: manual` fails | — | OPEN |
| Real end-to-end latency exceeds the ~1–2s budget | Weakens "intervene before execution" claim | Unknown | Spike steps 5–7 | Revisit which stage is slow via `AGENT_METRICS` breakdown | Narrow demo scenario to tolerate slightly higher latency | OPEN |
| Customer ID/Secret not yet provisioned in Console | Blocks `/join`/`/speak` REST calls entirely | Low-medium | Check Console before spike implementation | Provision before starting implementation | — | OPEN, cheap to close |
| ASR/TTS/LLM vendor undecided | Blocks concrete latency/quality expectations | Certain (not yet decided) | — | Decide before wiring extraction service | — | OPEN, non-blocking for spike |
| Live demo fragility (ASR/interruption failure during judged demo) | Demo failure in front of judges | Unknown | Rehearsal | Rehearse until timing is unremarkable | Recorded backup clip (recommended, not built) | OPEN |
| Time constraint (hard deadline 4 Sep 11:59 PM IST) | Scope overrun | Certain constraint | — | Strict MUST/SHOULD/IF-TIME/CUT discipline (§21) | — | Ongoing, actively managed |

---

## 24. VERIFIED / ASSUMED / NEEDS TESTING / BLOCKED

**VERIFIED**
- RTM is the correct transcript path (not Data Stream Mode).
- `remote_rtc_uids: ["*"]` subscribes to all humans (schema).
- Transcript event schema: `uid`, `turn_id`, `role`, `text`, `final`.
- `speak` payload: `{text, priority, interruptable}`; `priority: INTERRUPT/APPEND` documented behavior (contract, not live behavior).
- `interrupt_mode` controls agent's own interruption-reaction behavior (`interrupt`/`append`/`ignore`/`adaptive`/`keywords`).
- `AGENT_METRICS` enabled via `parameters.enable_metrics: true`, delivered as RTM event code `111`.
- Auth mechanism is Customer ID + Customer Secret (Basic Auth), not App ID + App Certificate.
- Agora project (`Xo4FYDFSa`) has `convoai` and `rtm` features enabled — hard blocker cleared.
- SDK-first (`agora-agents`) is the recommended integration path over hand-rolled REST.

**ASSUMED**
- `priority: IGNORE` as a third `speak` enum value (local-skill only, not independently corroborated against official docs).
- `turn_detection.config.start_of_speech.mode: "manual"` achieves the desired silence-by-default behavior (local-skill only, not in official-docs search results, not live-tested).
- Customer ID/Secret pair actually exists in the Console for this project (not confirmed either way).

**NEEDS TESTING**
- Multi-human UID attribution correctness under real overlapping speech.
- Actual acoustic behavior of `speak(priority=INTERRUPT)` while a human is actively talking.
- Whether `start_of_speech: manual` (or the LLM-slot-bypass fallback) actually keeps the agent silent through ordinary human-to-human conversation.
- Real (not estimated) end-to-end latency, utterance → audible intervention.
- Actual `AGENT_METRICS` payload shape once received live.

**BLOCKED**
- None currently — the Customer ID/Secret check is the only item that could become a hard blocker, and it hasn't been confirmed missing, only unconfirmed.

**MULTIMODAL EXTENSION (§29) — tracked separately, does not alter or block any item above:**

*ASSUMED*
- The chosen LLM provider's vision-capable mode can reliably read a named metric+value off a typical cloud-dashboard-style screenshot.

*NEEDS TESTING*
- Extraction accuracy across varied dashboard styles/resolutions/crops.
- Whether `extraction_certainty` (high/low) actually correlates with real extraction correctness.
- Behavior on a malformed/non-dashboard image (must degrade to "no evidence extracted," never a fabricated metric).
- Vision-mode extraction latency (off the voice pipeline's critical path — not gated by the ~1–2s intervention budget).

*NEEDS CONFIRMING*
- Whether the extraction LLM provider, once chosen (§12 — still NOT YET DECIDED), supports multimodal/vision input at all.

---

## 25. FINAL ARCHITECTURAL DECISIONS

| # | Decision | Reason | Alternatives rejected | Status |
|---|---|---|---|---|
| 1 | Track/problem fixed at PS4 | Hackathon rules forbid inventing a new problem statement | Any other track | LOCKED |
| 2 | REFACTOR, not KEEP or PIVOT | Core insight and LLM-parses/Python-decides separation are strong; only internals were weak | Full rebuild from scratch; keeping the original PPT's architecture as-is | LOCKED |
| 3 | Architecture B ("Grounded Commander") | Telemetry grounding catches humans being wrong about reality, not just inconsistent — hardest-to-replicate capability, strongest fit for PS4's "distinguish confirmed facts" requirement | Architecture A (plain veto machine), C (+real escalation integration), D (dashboard/text-only) | LOCKED |
| 4 | Three "engines" collapsed into one `risk_engine.evaluate()` | Avoids complexity-dressed-as-sophistication anti-pattern; keeps additional scope cheap (views/queries, not new systems) | Three separately-branded subsystems | LOCKED |
| 5 | Topology must be a real `networkx` graph with real traversal | Team's primary technical-depth proof point; original flat JSON was keyword-matching in disguise | Flat `topology.json` rule list | LOCKED |
| 6 | Telemetry grounding fixed at exactly 4 mocked metrics, Phase 2 only | Deliberate scope discipline against the ~2–3 day window; not open-domain | Real monitoring integration; open-domain telemetry | LOCKED |
| 7 | AEGIS's brain runs as an independent backend service | Avoids depending on Agora's default turn-taking/auto-reply semantics for suppression | Running extraction/state/risk inside Agora's built-in `llm` turn slot | LOCKED in principle; **exact silence mechanism is OPEN** — see below |
| 8 | AEGIS never executes/blocks/overrides | Non-negotiable safety/credibility boundary | Any autonomous execution path | LOCKED |
| 9 | Governor hard-rate-limits to ≤1 intervention/45s | Prevents alarm fatigue, protects demo stability | Unthrottled intervention | LOCKED |
| 10 | No real third-party tool integration | Keeps demo reliability high within the time window | Real Jira/Slack/PagerDuty integration (Architecture C) | LOCKED |
| 11 | Golden demo has two distinct reasoning beats | More convincing to judges than one capability shown twice | Single-beat demo (original PPT) | LOCKED |
| 12 | Build order: topology/risk engine first, live Agora integration last | Reasoning components are pure Python and independently testable; live-only unknowns could force an architecture change, so don't sink hours into live integration first | Live-integration-first | LOCKED |
| 13 | Agora spike must pass before any AEGIS intelligence-layer code that integrates with, or assumes verified, live Agora behavior is written (clarified: isolated, unit-tested components with no live-voice dependency — per §22 step 2 — may be built in parallel; only live-integration-dependent work must wait) | De-risk the two highest-priority unknowns (UID attribution, barge-in behavior) before investing further in code that depends on them | Building live-integration-dependent code in parallel with unresolved plumbing | LOCKED |
| 14 | Multimodal input (voice+text+image) generalizes `risk_engine.evaluate()`'s existing telemetry input into an `evidence[]` parameter; visual evidence is a new `Evidence` entity, distinct from `ExtractedClaim`; `extraction_certainty` is a categorical (high/low) flag, not a probabilistic score | Extends the already-planned "additional input producer to the same function" design (§4) without a new pipeline, a second AI system, or reopening the Bayesian/probabilistic-modeling non-goal (§26) | A separate image-understanding service/agent; collapsing evidence into the existing claim types; a numeric/probabilistic confidence score | LOCKED (added via the multimodal controlled revision, §29) |

**OPEN / UNRESOLVED:** Exact mechanism for keeping the agent silent-by-default — `turn_detection.config.start_of_speech.mode: "manual"` (local-skill discovery, not yet corroborated or tested) vs. bypassing the agent's `llm` slot entirely (original handoff plan). **This must be decided by spike results, not before.**

---

## 26. NON-GOALS

- No new problem statement outside PS4.
- No real third-party integrations (Jira/Slack/PagerDuty).
- No speaker diarization ML.
- No multiple named "engines" as separate services.
- No open-domain contradiction detection.
- No Bayesian/probabilistic confidence modeling.
- No multi-agent personas/voices.
- No autonomous execution of any action by AEGIS, ever.
- No production/scalability architecture — this is a hackathon prototype scoped to a live demo.
- No open-domain image understanding beyond named-metric, dashboard-style screenshot readings (§29) — no general-purpose computer vision, no OCR-as-a-service pipeline, no separate vision "engine" or agent.

---

## 29. MULTIMODAL INCIDENT INPUT — CONTROLLED EXTENSION

**Status:** LOCKED (as of this controlled revision). Extends, does not replace, the Agora-voice-centric architecture. Agora remains the primary real-time interaction mechanism (§1, unchanged).

**Scope boundary (explicit, to prevent future scope creep):** visual evidence is limited to metric/dashboard-style screenshots yielding one or more named metric+value readings (e.g., a cloud console panel showing "pool_utilization: 91%"). This is NOT general-purpose open-domain image understanding, NOT OCR-as-a-service, NOT a second AI system. It is one additional input modality handled by the existing LLM Structured Extraction service (§5 item 2).

**Conceptual convergence model:**
- **VOICE** → ASR transcript event → LLM Extraction service (text mode) → `ExtractedClaim` (fact/hypothesis/decision/proposed_action/confirmation/override/hold/none).
- **TEXT** (typed message submitted via the ingestion side-channel) → wrapped as a transcript-event-shaped object (`{uid: uploader_id, turn_id, role:"human", text, final:true}`) → same LLM Extraction service, same text mode, same `ExtractedClaim` schema. No new contract required for text — it is simply a second producer of the existing transcript-event shape.
- **IMAGE** (screenshot upload) → LLM Extraction service, vision-mode code path (same service; a different branch, not a different system) → `Evidence` object (new entity, distinct from `ExtractedClaim` — see §8).
- **TELEMETRY** (existing Phase 2 mock endpoint) → already-planned evidence input to `risk_engine.evaluate()`; this extension generalizes it into the same `Evidence` collection as image-sourced evidence, rather than keeping it a separate telemetry-only structure.
- All four converge on the same Incident State Store, and only `Evidence` and `proposed_action` claims are ever inputs to `risk_engine.evaluate()`.

**Why `Evidence` is a distinct entity from `ExtractedClaim`, not a fifth claim type:** a `fact`/`hypothesis`/`decision`/`proposed_action` is something a *human asserted*; `Evidence` is an *observation about the state of the world*, sourced either from the mocked telemetry endpoint or a human-submitted screenshot. Collapsing these would blur the exact distinction §3/§5.4 rely on for the product's core differentiation (grounding claims against reality, not just against each other) — a deliberate non-collapse.

**New component: Multimodal Evidence Ingestion (§5 item 10).**
- Responsibility: accept a typed text message or an uploaded screenshot from a participant during a live incident, outside Agora's voice channel, and feed it into the extraction pipeline.
- Inputs: raw text (via a lightweight side-channel — e.g. a form/box in the Core UI; not a new deployment target) or an image file.
- Outputs: for text — a transcript-event-shaped object; for image — a call into the Extraction service's vision-mode code path.
- State owned: none. Must NOT bypass the extraction/validation pipeline, treat an uploaded image as automatically authoritative, or execute/authorize anything.
- Explicitly not part of Agora's real-time transport — a deliberate side-channel, since this extension does not touch Agora's RTC/RTM layer.

**LLM Structured Extraction service — extended, not replaced (§5 item 11).**
- Text mode: unchanged, now fed by two producers (voice transcript, typed text) instead of one.
- Vision mode: new code path in the *same* service. Input: one image + optional short caption/context. Output: zero or more `Evidence` objects (§8). Uses the same reject-malformed-output/bounded-retry reliability pattern already specified for text mode — no new reliability framework.
- Provider note: confirm the chosen extraction LLM provider (§12) supports multimodal input; if not, this is the one new dependency this extension could introduce. **NEEDS CONFIRMING**, not assumed.

**`Evidence` schema:** see §8 (State Model) and §9 (Data Contracts) for the full field table.

**`risk_engine.evaluate()` — generalized input, same function, same check structure.**
- Signature generalizes from `(proposed_action, state, topology[, telemetry])` to `(proposed_action, state, topology, evidence[])`, where `evidence[]` includes both `mock_telemetry`-sourced and `screenshot_upload`-sourced entries. This is the same check already labeled "telemetry contradiction check (Phase 2 input, same function)" in §4 — only its input source is broadened. No new check, no new engine — decision #4 in §25 (one risk-engine function) is not reopened.
- `extraction_certainty: low` evidence must not, by itself, be sufficient grounds for a HIGH verdict — treated as grounds for at most an ASK-tier prompt to verify, not a WARN-tier hard claim. This is a deterministic, hard-coded branch on a categorical flag, not probabilistic modeling (decision #14, §25).
- When voice/text evidence (a spoken hypothesis) conflicts with `Evidence`-sourced data, the `Evidence` entry takes precedence in `reasons` — the same precedence the golden demo's beat 3 already implies (telemetry beats a spoken guess); this extension adds a second evidence source that can invoke the same rule, it does not change the rule.
- Missing/unreadable evidence (e.g. a blurry, unusable screenshot) is treated as "not evaluated," identically to the existing missing-telemetry failure behavior — never as evidence of LOW risk.

**State Store — one new collection (§8).** `evidence: Evidence[]`, appended to `timeline` alongside the existing collections. No change to `facts`/`hypotheses`/`decisions`/`proposed_actions` beyond the optional `source_modality` field on `ExtractedClaim` (§9).

**Explicitly out of scope for this extension (do not build) — see also §26:**
- Open-domain image understanding beyond named-metric dashboard readings.
- A second AI/vision "engine," service, or agent.
- Real-time image streaming or video.
- Automatic screenshot capture/scraping from external systems — all evidence is explicitly human-submitted.
- Any probabilistic/Bayesian confidence modeling over evidence or claims — `extraction_certainty` is categorical, not probabilistic.
- Any change to Agora's role, transport, or configuration.
- Any change to deployment — explicitly out of scope for this revision.

**VERIFIED / ASSUMED / NEEDS TESTING for this extension:** see §24's dedicated "MULTIMODAL EXTENSION" subsection — kept separate from the five Agora Critical Unknowns, which this extension does not alter or block.

**Phase placement (does not alter Phase 1's core voice gate):**
- **Phase 1:** no runtime multimodal behavior required. Only change: the State Store's schema includes the (initially unused) `evidence` collection and `ExtractedClaim.source_modality` field from the start, to avoid a later schema migration. Zero behavioral impact on Phase 1's completion gate.
- **Phase 2 (SHOULD BUILD, §21 item 10):** text ingestion, image ingestion, vision-mode extraction, `risk_engine.evaluate()` generalized to consume `evidence[]`, plus the evaluation additions below.
- **Phase 3 (IF TIME, §21 item 14, requires explicit team sign-off before touching the golden demo):** evidence UI treatment; an additional, optional demo beat (§20) demonstrating visual-evidence contradiction — not a replacement of the locked two-beat golden demo (§25 decision #11), and not to be added to the primary judged run without the team explicitly approving a change to the golden demo script, per the existing escalation rule (§27).

**Evaluation additions (Phase 2):** correct screenshot interpretation (exact-match rate against a hand-labeled screenshot set); incorrect screenshot interpretation (vision-mode hallucination rate); ambiguous screenshots (correct behavior is `extraction_certainty: low` or no evidence produced, never a confident wrong answer); conflicting voice vs. image evidence (does the engine correctly favor `Evidence` per the precedence rule); stale visual evidence (**NEEDS BASELINE** — reuse the existing hypothesis-staleness timestamp approach rather than inventing a new one); irrelevant images (zero `Evidence` objects produced); low-confidence visual evidence (verify the deterministic ASK-not-WARN branch fires); multiple evidence sources disagreeing with each other (**NEEDS DESIGN** — not resolved by this extension, flagged as an open question for the team); visual evidence contradicting a human claim (the primary demo scenario — should have the best evaluation coverage of the set).

**Demo (optional, IF TIME, requires sign-off) — see §20** for the full candidate-beat table. Summary: engineer states a metric verbally, a second participant uploads a dashboard screenshot showing a contradicting value, AEGIS extracts the visual evidence, `risk_engine.evaluate()` flags the contradiction, and the existing `speak(INTERRUPT)` mechanism delivers the intervention exactly as it already does for the mocked-telemetry case. This reuses 100% of the existing intervention/confirmation pipeline — multimodal input never gets its own intervention pathway.

---

## 27. ANTIGRAVITY HANDOFF

**What you are building:** The Agora real-time plumbing spike only — see §22 step 1. Not the AEGIS intelligence layer. Not the full pipeline. Just: two humans join a channel, AEGIS agent joins, transcripts are logged with UID, `/speak` intervention is tested including mid-human-speech, and real latency is captured via `AGENT_METRICS`.

**Current project state:** Agora project ready (`convoai`+`rtm` enabled, App ID known). No application code exists yet. Local Agora Skill and MCP already inspected once (see §11) — re-use those findings rather than re-searching from scratch unless something here looks stale.

**Frozen decisions you must not reopen:** §25, items 1–13. In particular: do not reintroduce three separate "engines," do not propose real third-party integrations, do not build the intelligence layer before the spike passes.

**What you may decide independently:** Exact file/folder layout for the spike; which of the two silence-mechanism candidates to try first (test `start_of_speech: manual` first since it's the simpler config change; fall back to the LLM-slot-bypass plan only if it fails); exact wording of the backend's minimal test endpoints.

**What requires escalation (ask before proceeding):** Whether the Customer ID/Secret pair needs to be provisioned in Console (check first, don't assume); any change to the golden demo script (§20); any deviation from the MUST-build list (§21) under time pressure — flag rather than silently cutting scope.

**Acceptance criteria for the spike:** All four success criteria from §24's "NEEDS TESTING" list resolved to either PASS or a documented, specific failure mode — not left ambiguous.

**Non-goals for this phase:** risk engine, topology, telemetry, incident state store, database, multi-agent architecture, production architecture — none of these are in scope until the spike passes (§22). The multimodal extension (§29) is also out of scope for the spike — it is a Phase 2 item.

---

## 28. FUTURE CLAUDE HANDOFF

### CONTEXT FOR FUTURE CLAUDE

- **Project:** AEGIS — voice-native Incident Commander for EchoSphere: Agora Conversational AI Hackathon, PS4 (Voice AI Incident Commander), team AI Slayers.
- **Problem:** Incidents fail from hedged guesses becoming fact, forgotten decisions, and destructive actions approved faster than anyone can catch consequences.
- **Final idea:** LLM understands → deterministic risk engine evaluates → human authorizes → Agora enables real-time voice intervention. Architecture B ("Grounded Commander") with telemetry grounding as a Phase 2 upgrade.
- **Final architecture:** §4–9 of this document.
- **Current implementation status:** **Zero code written.** Strategy and architecture are LOCKED. The immediate and only current task is the Agora real-time plumbing spike (§22 step 1).
- **Key technical decisions:** §25 — read before proposing anything that resembles a redesign.
- **Agora setup:** §11 — project ready, `convoai`/`rtm` enabled, App ID known, local Skill/MCP already inspected once.
- **Remaining work:** Full build order in §22.
- **Known risks:** §23.
- **Known uncertainties:** §24 — this is the active checklist; nothing in it should be assumed resolved without a specific test result.
- **What has already been decided:** All of §25's LOCKED items.
- **What must NOT be reconsidered:** The track (PS4), the REFACTOR-not-pivot call, Architecture B, the one-risk-engine-function collapse, the never-execute safety boundary, the no-real-third-party-integration decision. Do not re-run divergent ideation (§3's brainstorm skill process) — it already happened and converged.
- **Multimodal extension:** §29 — voice+text+image input, controlled revision, LOCKED. Read before proposing anything that looks like a second AI/vision system; the design already generalizes the existing telemetry-input slot instead.

**If you are a session without local file/MCP access:** say so explicitly before attempting to inspect `.agents/skills/agora/` or claim Agora MCP access — this exact gap has already caused confusion once in this project's history (see §11). Do not assume access without checking.

---

# FINAL PROJECT STATUS

### Strategy
LOCKED

### Architecture
LOCKED

### Implementation
NOT STARTED — zero code written. Immediate task is the Agora real-time plumbing spike.

### Critical Unknowns
1. Multi-human UID attribution correctness under real overlapping speech.
2. Actual acoustic behavior of `speak(priority=INTERRUPT)` during live human speech.
3. Whether `start_of_speech: manual` (vs. the LLM-slot-bypass fallback) actually achieves silence-by-default.
4. Real end-to-end intervention latency.
5. Whether a Customer ID/Secret pair exists in Console for raw REST auth.

*(The multimodal extension, §29, has its own separate ASSUMED/NEEDS TESTING/NEEDS CONFIRMING items — see §24's dedicated subsection. They are a Phase 2 concern and do not alter or block the five Critical Unknowns above, which remain the immediate spike's acceptance criteria.)*

### Immediate Next Engineering Step
Execute the Agora real-time plumbing spike exactly as scoped: two humans join a channel, AEGIS agent joins with `remote_rtc_uids: ["*"]` and RTM enabled, transcripts are logged with UID, `/speak` intervention is tested both in silence and mid-human-speech, and `AGENT_METRICS` plus app-side timestamps are captured for real latency numbers. No AEGIS intelligence-layer code until this passes.

### Source of Truth
This document is the authoritative project reference unless a future architectural decision is explicitly approved and this document is updated accordingly.
