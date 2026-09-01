# AEGIS — MASTER IMPLEMENTATION BLUEPRINT

*Produced by the Implementation Prompt Architect, from `AEGIS_SSOT.md` (status Sep 1 2026). Strategy and architecture are LOCKED and are not reopened here. This document answers HOW to build the frozen design, not WHAT to build.*

*Revised to incorporate the multimodal incident input extension (SSOT §29). Only the sections below marked with a multimodal note were changed; everything else is unmodified from the original blueprint. See `AEGIS_MULTIMODAL_CHANGE_IMPACT_AND_DESIGN.md` for the full change rationale.*

---

## 1. Implementation Objective

Convert the frozen AEGIS design into an unambiguous, three-phase, dependency-ordered engineering plan, and produce one copy-paste-ready prompt for Antigravity that can execute all three phases without re-deciding architecture.

**Note on scope vs. SSOT §27:** SSOT §27 ("Antigravity Handoff") scoped Antigravity's *immediate* task to the spike only. This blueprint supersedes that narrow scoping at the user's explicit request — it plans the full build — but it **preserves the sequencing constraint that produced §27**: the spike is still Phase 1's first executed step, and no code that depends on *live* Agora behavior is written before the spike passes. This is not a redesign; it is the same build order (§22) expressed across three phases instead of one.

---

## 2. Current Project State

- Zero application code exists. Clean workspace.
- Agora project (`Xo4FYDFSa`, App ID `1dbe4a73adb1480086fedd84493f92a7`) has `rtc`, `rtm`, `convoai` enabled and confirmed ready (`agora project doctor --feature convoai`).
- Local Agora Skill inspected once (`.agents/skills/agora/`), Agora MCP available *in the Antigravity environment only* — not in this chat session.
- Five critical unknowns are open (SSOT §24/"Critical Unknowns") — all five must resolve to PASS or a documented failure mode before any live-voice-dependent code is trusted.
- Hard deadline: 4 Sep 2026, 11:59 PM IST. As of this document, roughly 3 days remain — this is a hackathon prototype, not a production build, and every downstream decision in this blueprint is made under that constraint.
- Customer ID/Secret existence for REST Basic Auth is **unconfirmed** — this is a candidate hard blocker for `/join` and `/speak` and must be checked before any Agora REST wiring, not assumed.

---

## 3. Frozen Architecture (restated, not reopened)

```
Agora Voice (multi-party RTC, remote_rtc_uids: ["*"])
   → RTM transcript delivery
   → LLM Structured Extraction (independent backend service)
   → Incident State Store (facts, hypotheses+staleness, decision ledger, ownership, timeline, proposed actions)
   → risk_engine.evaluate(proposed_action, state, topology, evidence[])   — ONE function (generalized from telemetry-only to telemetry+visual evidence, SSOT §29)
   → Intervention Governor (SILENT→SUGGEST→ASK→WARN, ≤1 speak/45s)
   → Agora speak (priority: INTERRUPT) if WARN/ASK
   → Human spoken confirm/deny/hold (same ASR→extraction pipeline)
   → State Store updated; proposed action resolved

[Multimodal, Phase 2+, SSOT §29] TEXT / IMAGE side-channel → same Extraction service (text mode / vision mode) → ExtractedClaim or Evidence → same State Store → same risk_engine.evaluate() call above. No new pipeline.
```

Non-negotiable boundary, to be preserved in every component built in every phase:

> **AI interpretation ≠ deterministic authorization ≠ human authorization ≠ execution**

AEGIS never executes, blocks, or overrides an action. Full list of LOCKED decisions: SSOT §25, items 1–13. None are reopened by this blueprint.

---

## 4. PHASE 1 — MVP / Core System

### Objective
A complete, working, *live* AEGIS loop: incident conversation → understanding → state → evaluation → intervention → human response → updated state. Not polished. Not evaluated rigorously yet. Functional, end to end, on the real Agora stack.

### Architecture for this phase
All components from §3 exist as thin, correct implementations. Per SSOT decision #13 (§25, as clarified), the Agora spike must resolve the five Critical Unknowns before any *live-integration-dependent* implementation proceeds — code that wires into, or assumes verified, live Agora behavior. Isolated, unit-tested construction that neither depends on nor assumes any unverified Agora behavior may proceed during the spike; this is real implementation work, not merely planning, but it stays integration-independent. Two tracks reflect this split and only merge at step 7:

- **Track A (live-integration-dependent, must wait on the spike):** Agora RTC/RTM plumbing, `/join`, `/speak`, live UID attribution, live barge-in — anything that depends on or assumes verified live Agora behavior.
- **Track B (isolated construction, no dependency on or assumption about live Agora behavior — may proceed during the spike):** topology graph + risk engine, Incident State Store, extractor (tested offline against a fixed written transcript, never live audio), Governor, and their text-only end-to-end wiring — all built and validated without any live voice input or any assumption about how the spike will resolve.

Track A and Track B converge only at "Live Agora integration" (step 7 below), which does not begin until the spike (step 1) has produced a PASS or documented-failure result for all five Critical Unknowns. Nothing in Track A may contain AEGIS reasoning logic (extraction, risk, governor); nothing in Track B may be wired to live Agora transcript/`speak` calls until step 7. If spike results force a change to a live-behavior assumption (e.g. the silence mechanism, or `speak`'s acoustic behavior), Track B's components are unaffected, since none of them assume a specific answer to those unknowns — this is why they are safe to build in parallel rather than a violation of the spike-first constraint.

### Components (Phase 1 scope)

**1. Agora RTC/RTM/TTS layer**
- Responsibility: get human audio into a multi-party channel with per-speaker attribution, deliver transcript events over RTM, deliver `speak` broadcasts.
- Inputs: human audio, `speak` REST/SDK calls.
- Outputs: transcript events (`uid`, `turn_id`, `role`, `text`, `final`), `AGENT_METRICS` (event code 111), broadcast audio.
- State owned: none.
- Must NOT: make risk decisions, execute or gate any action, hold intelligence-layer state.
- Verification order (this **is** the spike, SSOT §22 step 1 / §27):
  1. Two humans join distinct RTC UIDs; AEGIS agent joins with `remote_rtc_uids: ["*"]`, RTM enabled (`advanced_features.enable_rtm: true`, `parameters.data_channel: "rtm"`).
  2. Confirm Customer ID/Secret exist in Console (Developer Toolkit → RESTful API) — **check before writing any REST call**, per SSOT §11/§23. If absent, provision before proceeding; this is the one candidate hard blocker in Phase 1.
  3. Log transcript events from both humans speaking normally, including overlapping speech; inspect `uid` correctness. → resolves Critical Unknown #1.
  4. Attempt the silence-by-default candidate: `turn_detection.config.start_of_speech.mode: "manual"` first (simpler config change, per SSOT §27 autonomy grant). If it does not suppress unsolicited agent turns through ordinary human-to-human conversation, fall back to the documented alternative (bypassing the agent's built-in `llm` slot). → resolves Critical Unknown #3.
  5. Call `speak(priority: INTERRUPT)` (a) in silence, (b) while a human is actively talking. Record acoustic behavior. → resolves Critical Unknown #2. If `INTERRUPT` does not reliably cut through, this is a **TECHNICAL BLOCKER** against the "intervene before execution" premise (SSOT §23) — do not silently soften the claim; report it and use the documented fallback framing ("delivers immediately, can in principle be talked over").
  6. Enable `parameters.enable_metrics: true`; capture `AGENT_METRICS` payload shape live, plus app-side timestamp-at-`speak`-call vs. timestamp-at-audible-TTS, across multiple calls (not one sample). → resolves Critical Unknowns #4 and the payload-shape item.
  - **Spike completion gate:** all five Critical Unknowns (SSOT "Critical Unknowns" list) resolved to PASS or a documented, specific failure mode. Ambiguous results are not acceptable as "done."

**2. LLM Structured Extraction service**
- Responsibility: convert one final transcript utterance (+ recent context) into zero or more typed claims.
- Inputs: `{uid, turn_id, role, text, final}` from RTM (or, for offline testing, a fixed written transcript file with the same fields).
- Outputs: `ExtractedClaim[]` (schema below).
- State owned: none (stateless pass-through; may hold a small rolling context window in memory, but does not own the Incident State Store).
- Must NOT: decide risk/safety; assert that its own output is confirmed fact; write directly to state without going through the Store's validation path.
- **Data contract — `ExtractedClaim` (schema NOT finalized in SSOT; finalized here within the frozen conceptual model of SSOT §8):**

  | Field | Type | Required | Notes |
  |---|---|---|---|
  | `claim_id` | string (UUID) | yes | generated by the extraction service |
  | `type` | enum: `fact` \| `hypothesis` \| `decision` \| `proposed_action` \| `confirmation` \| `override` \| `hold` \| `none` | yes | `none` = utterance contained no extractable claim; extractor must emit this explicitly rather than omitting output, so the pipeline can log "heard, nothing extracted" distinctly from "extraction failed" |
  | `text` | string | yes | normalized claim text, not a verbatim transcript echo |
  | `speaker_uid` | string | yes | copied from the transcript event, never inferred |
  | `timestamp` | ISO-8601 string | yes | copied from transcript event time, not extraction-call time |
  | `source_turn_id` | string | yes | for traceability back to the raw transcript event |
  | `target_ref` | string \| null | conditional | required when `type == proposed_action` (e.g. `"core-db"`); null otherwise |
  | `metric_ref` | string \| null | optional | present when the claim references a telemetry metric by name (Phase 2 consumer) |
  | `ownership_tag` | string \| null | optional | free text, per SSOT §8 |
  | `source_modality` | enum: `voice` \| `text` | optional (Phase 2+, SSOT §29) | provenance only — text-sourced claims use this identical schema and validation path; no functional impact on risk logic |

  Producer: extraction service. Consumer: Incident State Store. Validation: reject (log + drop, do not crash) any object missing a required field or with an out-of-enum `type`; on rejection, emit `type: "none"` downstream so the loop is not silently blind.

- **Failure behavior:** invalid/malformed LLM output → validated and rejected per above, not passed to state. LLM API failure/timeout → retry once with backoff (bounded, hackathon-scoped — do not build a general retry framework); on second failure, emit `type: "none"` and log the failure; never block the RTM ingestion loop waiting on a stuck call.
- **Offline test (SSOT §22 step 4, part of Phase 1):** run the service against a fixed written transcript (author it to cover at least one instance of each claim type) with no live audio in the loop. This validates the contract before Track A exists.

**3. Incident State Store**
- Responsibility: single source of truth for facts, hypotheses (with staleness/reinforcement), decision ledger, ownership, timeline, proposed actions.
- Inputs: `ExtractedClaim` objects; human decisions (`confirmation`/`override`/`hold` claims, same contract).
- Outputs: current state snapshot (read API for the risk engine, UI, and status-summary feature).
- State owned: everything listed above. Persistence: SQLite (per SSOT §12, "decision made in principle, not yet implemented") for demo crash-safety; an in-memory mirror is acceptable for latency but SQLite must be the durable copy.
- Must NOT: execute or authorize anything; collapse `fact`/`hypothesis`/`decision`/`proposed_action` into one undifferentiated "note" type — SSOT §10 explicitly forbids this collapse.
- **Schema (finalizing SSOT §8's "NEEDS DESIGN" state model):**

  - `facts`: append-only list of `{claim_id, text, speaker_uid, timestamp}`. No staleness field — facts do not go stale in this model.
  - `hypotheses`: `{claim_id, text, speaker_uid, timestamp, reinforcement_count, last_touched_at, status: active|stale}`. `reinforcement_count` increments when a later claim restates/supports it (extraction-service responsibility to flag this via `target_ref` or repeated `text` similarity — Antigravity may choose the matching heuristic, this is non-critical implementation detail per skill rule §26).
  - `decisions`: append-only Decision Ledger, `{claim_id, text, speaker_uid, timestamp}`.
  - `proposed_actions`: `{claim_id, target_ref, speaker_uid, timestamp, status: pending|confirmed|declined|held, risk_verdict: RiskVerdict|null, resolved_by_uid: string|null, resolved_at: timestamp|null}`.
  - `timeline`: append-only ordered log of every claim, for the final summary and any UI.
  - `evidence` (Phase 2+, SSOT §29): `{evidence_id, source_type: telemetry|visual, metric_name, value, unit, extraction_certainty: high|low, source: mock_telemetry|screenshot_upload, uploader_uid, timestamp, target_ref, raw_reference}`, also appended to `timeline`. **Include this collection's schema in Phase 1's initial migration even though it stays empty/unused until Phase 2** — avoids a later schema migration; zero behavioral impact on the Phase 1 gate.
- **Conflict/staleness rule (NEEDS DESIGN per SSOT §8 — finalized here, hackathon-scoped, simple and defensible):** a hypothesis is considered contradicted (by a later fact, telemetry, evidence, or an explicit newer hypothesis on the same `target_ref`/topic) when the risk-evaluation logic determines this and *returns* that determination as part of its output. `risk_engine.evaluate()` remains a pure function at all times — it never writes to the State Store, here or anywhere else. The State Store/application layer is what applies the returned determination, by setting the hypothesis's `status` field to `stale`. This preserves the "deterministic engine decides, application layer applies" boundary: the engine computes and returns a value; it does not mutate state directly.
- **Failure behavior:** duplicate `claim_id` → idempotent no-op (do not double-append). Out-of-order timestamps (RTM delivery is not guaranteed strictly ordered) → sort by `timestamp` on read for the timeline view; do not assume write order equals chronological order.

**4. Topology / Risk Evaluation — `risk_engine.evaluate()`**
- Responsibility: the *only* function that makes a risk/safety determination.
- Inputs: one `proposed_action`, current state snapshot, topology graph (Phase 1: static `networkx` graph, ~8–12 nodes, edges typed `depends_on`/`reads_schema`/`compatible_with`). **Phase 2+ (SSOT §29): signature generalizes to also accept `evidence[]`** (superset of `mock_telemetry`- and `screenshot_upload`-sourced `Evidence` objects) — this is the same function, same call site, one broadened parameter.
- Outputs: `RiskVerdict = {risk_tier: LOW|MEDIUM|HIGH, reasons: string[]}`. `reasons` must name the specific rule/evidence gap violated (the demo script in SSOT §20 requires AEGIS to state exact reasons, e.g. "schema v17 incompatible with v2.3" — this is a hard requirement flowing from the demo, not decoration).
- State owned: none — pure function over what it's given. Must be independently unit-testable with no voice, no LLM, no network.
- Internal checks (Phase 1 must implement all except telemetry/evidence, which is Phase 2 per SSOT §25 decision #6):
  1. **Staleness check** — is the `proposed_action`'s justifying hypothesis marked stale, or has it never been reinforced/confirmed?
  2. **Decision-reversal check** — does this action contradict an existing Decision Ledger entry with no new evidence since?
  3. **Topology blast-radius check** — BFS/DFS over the `networkx` graph from the action's `target_ref`; if any dependent node's `compatible_with`/`reads_schema` edge is violated by the action, flag it and name the specific downstream node and edge in `reasons`.
  4. **Evidence-contradiction check (Phase 2+, generalized from telemetry-only per SSOT §29)** — does any `Evidence` entry (telemetry or visual) contradict the claim justifying this action? `extraction_certainty: low` visual evidence is grounds for at most an ASK-tier verdict, never WARN, by itself — a deterministic branch on the categorical flag, not probabilistic weighting.
- Must NOT: call an LLM to make the decision; leave `reasons` empty on a non-LOW verdict.
- **Test order (SSOT §22 step 2):** build and unit-test standalone, before or in parallel with the spike, no voice dependency. This is the team's primary technical-depth proof point (SSOT §5.4) — it deserves real test coverage, not a stub.

**5. Intervention Governor**
- Responsibility: turn a `RiskVerdict` into a decision about whether/how to speak.
- Inputs: `RiskVerdict`.
- Outputs: `GovernorDecision = SILENT | SUGGEST | ASK | WARN` (+ the text to speak, when not SILENT).
- State owned: the rate-limit timer (last-intervention timestamp).
- Must NOT: bypass the ≤1-per-~45s hard rate limit under any circumstance, including a second HIGH-risk event arriving inside the window — SSOT §16/§25 decision #9 is explicit that this is a demo-stability and alarm-fatigue boundary, not a soft default.
- **Failure/edge behavior:** if two verdicts arrive inside the rate-limit window, the second is queued and re-evaluated for continued relevance when the window opens (do not silently drop it, and do not speak it as if it were fresh if the state has since changed — re-run `risk_engine.evaluate()` against current state before speaking a queued verdict).
- **Test order (SSOT §22 step 5):** tested against synthetic (hand-authored) `RiskVerdict` objects, no live pipeline needed.

**6. Agora `speak` call (intervention delivery)**
- Responsibility: deliver Governor output audibly.
- Contract: `POST /v2/projects/{appid}/agents/{agentId}/speak`, body `{text (≤512 bytes), priority: INTERRUPT|APPEND, interruptable: bool}`. `priority: IGNORE` is **ASSUMED**, not independently verified against official docs (SSOT §24) — if Antigravity finds it undocumented officially, do not rely on it for anything load-bearing in the golden demo; only use `INTERRUPT`/`APPEND`, which are verified.
- Must NOT: execute or block the underlying action — it only speaks.

**7. Human Confirmation capture**
- Responsibility: capture the human's spoken reply and classify it.
- Same ASR→extraction pipeline as everything else; the extraction service's `confirmation|override|hold` claim types (already in the `ExtractedClaim` schema above) cover this — there is no separate component to build, only a routing rule: when a `proposed_action` is `pending`, the next classified `confirmation`/`override`/`hold` claim from any participant resolves it.
- **Fail-safe (SSOT §16 — "NOT YET DESIGNED" timeout/ambiguity logic, finalized here):** absence of a clear confirm/deny/hold within a bounded window (Antigravity may pick a reasonable demo-scoped value, e.g. on the order of the rate-limit window) leaves the action `pending`, never auto-resolves to confirmed. No response = no authorization, always.

**8. Core UI**
- Minimum for Phase 1: a live view of the current Incident State Store snapshot (facts/hypotheses/decisions/proposed actions), refreshed on state change. No visual polish required yet (Phase 3).

**9. Observability/logging (basic)**
- Log every: transcript event received, claim extracted (or rejected), state mutation, risk verdict, governor decision, `speak` call made, human resolution. Enough to reconstruct what happened after a demo run — this doubles as Phase 2's evaluation input, so don't build a throwaway logger; use one structured log format from the start.

**10. Multimodal Evidence Ingestion (Phase 2+, SSOT §29 — not Phase 1)**
- Responsibility: accept a typed text message or an uploaded screenshot from a participant, outside Agora's transport, and feed it into the extraction pipeline.
- Inputs: raw text (via a lightweight side-channel, e.g. a form/box in the Core UI) or an image file.
- Outputs: for text — a transcript-event-shaped object (`{uid: uploader_id, turn_id, role:"human", text, final:true}`), fed into the extraction service's existing text mode; for image — a call into a new vision-mode code path in the *same* extraction service, producing `Evidence` objects.
- State owned: none.
- Must NOT: bypass extraction/validation; treat an uploaded image as automatically authoritative; execute or authorize anything; become a second AI/vision system — it is one new code branch in component 2, plus a minimal ingestion side-channel.
- **Evidence schema:** `{evidence_id, source_type: telemetry|visual, metric_name, value, unit, extraction_certainty: high|low, source: mock_telemetry|screenshot_upload, uploader_uid, timestamp, target_ref, raw_reference}`. `extraction_certainty` is categorical, not a probabilistic score — deliberately, to avoid reopening SSOT §26's "no Bayesian/probabilistic confidence modeling" non-goal. Deliberately no "relationship to existing claims" field — that relationship is computed by `risk_engine.evaluate()` at evaluation time and surfaces in `RiskVerdict.reasons`, not stored redundantly here.
- **Scope boundary:** visual evidence is limited to named-metric, dashboard-style screenshot readings — not open-domain image understanding.
- **Failure behavior:** same reject-malformed-output/bounded-retry pattern as text-mode extraction (component 2) — no new reliability framework. Missing/unreadable evidence is "not evaluated" by the risk engine, never treated as LOW risk.
- **Provider note:** confirm the chosen extraction LLM provider (component 2) supports vision input before building this; if not, flag the gap rather than silently substituting a different provider. **NEEDS CONFIRMING.**

### Data Flow (Phase 1 end-to-end)

```
Human speech
  → Agora RTC/ASR
  → RTM transcript event {uid, turn_id, role, text, final}
  → Extraction service → ExtractedClaim[] (validated)
  → Incident State Store (facts/hypotheses/decisions/proposed_actions/timeline updated)
  → [if proposed_action] risk_engine.evaluate() → RiskVerdict
  → Intervention Governor → GovernorDecision
  → [if not SILENT] Agora speak(priority: INTERRUPT)
  → Human spoken reply
  → (loop: same pipeline) → confirmation/override/hold claim
  → proposed_action.status resolved; State Store updated
```

### Implementation Sequence (Phase 1)

1. Agora spike (component 1, Track A) — **and, in parallel, starting immediately (Track B: isolated construction only, no live-Agora dependency or assumption):**
2. Topology graph + `risk_engine.evaluate()`, unit-tested standalone (Track B).
3. Incident State Store, tested independently (Track B).
4. Extractor, tested offline against a fixed written transcript (Track B).
5. Governor, tested against synthetic verdicts (Track B).
6. Text-only pipeline wired end-to-end (feed the written transcript through extractor → state → risk → governor → console output; no audio). **Checkpoint:** this must produce the SSOT §20 golden-demo beats correctly on text input before any live audio is attempted.
7. Live Agora integration — **gated on step 1 passing** (all five Critical Unknowns resolved). Wire Track A's transcript events into Track B's already-tested pipeline; wire Governor output into `speak`.
8. First live rehearsal pass of the golden demo script (SSOT §20) — full pass/fail, not yet polished (that's Phase 3).

### Testing (Phase 1)
- Unit: risk engine (all three checks, including at least one designed-to-trigger-HIGH case per check), Governor state machine, State Store mutations and idempotency.
- Integration: extractor → State Store, using the fixed written transcript.
- Real-time: the six spike verification steps above.
- End-to-end: text-only golden-demo pass (step 6), then live golden-demo pass (step 8).

### Acceptance Criteria (Phase 1)
- All five Critical Unknowns resolved to PASS or documented failure mode, with the resolution recorded (not left implicit).
- `risk_engine.evaluate()` correctly returns HIGH with a specific, correct `reasons` entry for the SSOT §20 beat-6 scenario (stale hypothesis + blast-radius) when run against the fixed test transcript.
- A full live run of the golden demo's non-telemetry path succeeds: staleness detection, decision-reversal detection, and topology blast-radius (the core of beat 6) all fire correctly with AEGIS speaking, and a spoken status summary is produced on "AEGIS, status?" (beat 8) — even if not yet reliable across repeated runs (that reliability bar is Phase 2/3). Beat 3 (telemetry contradiction) is **not** in scope for Phase 1's gate — it depends on `mock_telemetry.py`, a Phase 2 deliverable — so the full two-killer-moment demo is a Phase 2 acceptance item, not a Phase 1 one.
- No action is ever auto-authorized without an explicit human confirm/deny/hold claim, verified by test.

### Phase 1 Completion Gate
The full loop (conversation → understanding → state → evaluation → intervention → human response → updated state) runs at least once, live, end to end, and produces both golden-demo killer moments. Do not begin Phase 2 until this gate passes — an unreliable-but-functional loop is the Phase 1 target; a reliable one is Phase 2's job.

---

## 5. PHASE 2 — Intelligence / Reliability / Evaluation

### Objective
Make the functional MVP accurate, robust, measurable, and technically defensible — turn "it worked once" into "we can show it works and know when it doesn't."

### Improvements to plan

**Accuracy**
- Fact/hypothesis classification: review extraction outputs from repeated runs of the fixed transcript plus new adversarial transcripts (see Evaluation below); tighten the extraction prompt/schema validation based on observed misclassifications. No prompt content is specified here — that is legitimate Antigravity-owned implementation detail (skill §26).
- Participant attribution: re-verify under Phase 2's more adversarial multi-speaker tests (overlapping speech, fast interruptions) — this was only spot-checked in the Phase 1 spike.
- Decision extraction / contradiction detection / staleness handling: exercise each of the risk engine's three checks against deliberately adversarial proposed actions (a decision reversed with no new evidence; a hypothesis restated verbatim vs. genuinely reinforced with new evidence; a blast-radius edge case at graph boundary nodes).
- Intervention precision / false-intervention reduction: track false positives (Governor speaks when a human reviewer would judge SILENT correct) and false negatives (Governor stays silent on a genuine HIGH case) across the evaluation set below; adjust risk-tier thresholds, not the architecture.

**Telemetry grounding (SSOT decision #6, Phase 2 by design):** add the 4 fixed mocked metrics (`pool_utilization`, `error_rate`, `p99_latency`, `schema_version`) via `mock_telemetry.py`, feeding into `risk_engine.evaluate()` as an additional input producer to the *same* function — not a new pipeline, not a new engine. This is what unlocks golden-demo beat 3.

**Multimodal evidence grounding (SSOT §29, same tier as telemetry grounding, same reasoning — SHOULD BUILD):** build component 10 (Multimodal Evidence Ingestion) and the vision-mode extraction code path; generalize `risk_engine.evaluate()`'s signature to `evidence[]` as specified in component 4's Phase 2 note above; implement the deterministic `extraction_certainty: low` → ASK-not-WARN branch. **Confirm the chosen LLM provider supports vision input before building — flag the gap if not, don't silently substitute.** This is text/image evidence's only Phase 2 requirement; Phase 1 required no runtime change for this capability, only the empty schema.

**Evaluation infrastructure**
- Metrics to track (only those meaningful for this system; do not invent precision numbers before measuring):
  - Extraction accuracy (claim correctly typed and text-normalized) against a hand-labeled transcript set.
  - Attribution accuracy (`speaker_uid` correct) — primarily a live-voice metric, re-measured from Phase 1's spike method.
  - Risk detection precision/recall against hand-authored proposed-action scenarios with known correct verdicts.
  - False-positive intervention rate / false-negative intervention rate.
  - Decision-reversal detection accuracy.
  - End-to-end latency (utterance → audible intervention), distribution across many `speak` calls, not one sample — reusing the Phase 1 `AGENT_METRICS` + app-timestamp method.
  - Intervention latency specifically (Governor decision → `speak` call issued).
- Evaluation datasets: (1) the Phase 1 fixed written transcript, extended with adversarial variants (ambiguous hedges, reversed decisions, near-miss blast-radius cases); (2) 2–3 additional live-voice adversarial scenarios beyond the golden demo, run and logged the same way as the spike.
- Ground truth: hand-labeled by the team against the hand-authored scenarios above — this is a hackathon-scoped evaluation set, not a large benchmark; say so honestly if judges ask.
- Automated evaluation: a script that replays the fixed/adversarial transcripts through the text-only pipeline (Phase 1 step 6's harness) and diffs actual vs. expected claims/verdicts.
- Manual evaluation: live-voice adversarial runs, human-judged.
- Adversarial scenarios: at minimum — overlapping speech, a decision reversed twice, a hypothesis that should NOT be flagged stale (correctly reinforced), a proposed action that is genuinely LOW risk (to test the Governor doesn't over-trigger).
- Regression testing: re-run the fixed transcript suite after every change to extraction, risk engine, or Governor; a change that flips a previously-correct verdict is a regression, not a fix, until proven otherwise.
- **Multimodal evaluation additions (SSOT §29):** correct screenshot interpretation (exact-match on metric+value against a hand-labeled screenshot set); incorrect interpretation (vision-mode hallucination rate); ambiguous screenshots (correct behavior is `extraction_certainty: low` or no evidence produced, never a confident wrong answer); conflicting voice vs. image evidence (verify the engine favors the `Evidence` entry, per SSOT §29's precedence rule); stale visual evidence (**NEEDS BASELINE** — reuse the existing hypothesis-staleness approach); irrelevant images (zero `Evidence` objects produced); low-confidence evidence (verify the ASK-not-WARN branch fires); two disagreeing evidence sources (**NEEDS DESIGN**, not resolved by this extension — flag to the team, don't silently pick a rule).

**Reliability**
- Invalid LLM outputs: already handled by the Phase 1 validation/reject path — Phase 2 adds logging/counting of rejection rate as an evaluation metric.
- API failures / timeouts / retries: bounded retry (already specified in Phase 1) — Phase 2 verifies this doesn't silently degrade the demo (e.g. a retry storm delaying the pipeline past the intervention-before-execution premise).
- Duplicate events / ordering: State Store idempotency (Phase 1) and sort-by-timestamp (Phase 1) are exercised under Phase 2's adversarial live tests.
- Concurrent speech / participant ambiguity: re-tested under Phase 2's harder multi-speaker scenarios (this is the same Critical Unknown #1 area, now under harder conditions than the spike's basic check).
- Disconnect/reconnect: define expected behavior — on RTC/RTM disconnect, the agent should not silently lose state; State Store persistence (SQLite) is what protects this. Define and test at least one disconnect/reconnect cycle mid-incident.
- State inconsistencies: covered by the Store's conflict/staleness rule (Phase 1) — Phase 2 adds a test for two proposed actions on the same `target_ref` arriving close together.
- External dependency failures: LLM provider outage → same reject/`type:none` path as invalid output; mock telemetry endpoint down → risk engine must treat missing telemetry input as "not evaluated," not as "clean" (a missing metric is not evidence of LOW risk — this is a correctness-critical distinction and must not default to silently approving).

**Performance**
- Measure before optimizing: use Phase 1's `AGENT_METRICS` + app-timestamp instrumentation, already in place, to identify the actual bottleneck stage (ASR / extraction LLM call / state+risk / TTS) rather than guessing. Only optimize the stage the data implicates.
- If a number cannot yet be measured, mark it **NEEDS BENCHMARKING**, not estimated.

### Testing (Phase 2)
Regression suite (automated, from the evaluation dataset), adversarial live-voice manual runs, reliability tests for each failure mode above (at minimum one deliberate trigger per failure mode listed).

### Acceptance Criteria (Phase 2)
- Extraction/risk/attribution metrics measured and recorded (actual numbers, not estimates) against the evaluation set.
- False-positive and false-negative intervention rates measured on the adversarial scenario set, with at least one round of threshold adjustment based on the first measurement.
- All reliability failure modes listed above have been deliberately triggered at least once and produce the specified (non-crashing, non-silently-wrong) behavior.
- Regression suite passes after the Phase 2 changes.
- Real end-to-end latency number reported (not estimated) with distribution, not a single sample.
- With `mock_telemetry.py` wired in, a full live run produces both killer demo moments (beat 3 telemetry contradiction compounding into beat 6) as specified in SSOT §20.

### Phase 2 Completion Gate
The system's accuracy, false-intervention rate, and reliability behavior are backed by measured evidence, not belief — every number in the team's judging pitch (SSOT §19) is one the team has actually produced, not estimated.

---

## 6. PHASE 3 — Polish / Demo / Hackathon Readiness

### Product Polish
- UI: incident state visualization (facts/hypotheses/decisions/timeline), decision ledger view, risk visualization tied to `RiskVerdict.reasons`, confidence/staleness indicators on hypotheses, intervention history log, final incident summary view. All of this reads from the Phase 1/2 State Store's existing schema — **no new state fields invented for the UI's sake**; if the UI needs a field the Store doesn't have, that's a Phase-1/2 contract gap to fix at the source, not a UI-layer workaround.
- Ownership: surface the `ownership_tag` field already in the claim schema; do not add new extraction responsibility here.
- Only elements consistent with the SSOT are built — no scope creep into features not in SSOT §21's MUST/SHOULD/IF-TIME lists.
- **IF TIME (SSOT §21 item 14, SSOT §29):** evidence UI treatment — thumbnail + extracted metric/value shown alongside telemetry in the risk visualization, reading from the `evidence` collection already in the State Store schema. Same "no new state fields for the UI's sake" rule applies.

### Demo Engineering
Exact flow is SSOT §20, verbatim — this blueprint does not alter a single beat. Reproduced here for Antigravity's direct use:

- Initial state: two teammates, distinct RTC UIDs (e.g. `1001`, `1002`), AEGIS agent joined and subscribed to both.
- Beat-by-beat script, detected claims, internal processing, and expected AEGIS lines: SSOT §20 table, beats 1–9.
- The single KILLER DEMO MOMENT is beat 3 (telemetry contradiction) compounding into beat 6 (stale hypothesis + blast-radius) — this sequencing is load-bearing for the pitch and must not be reordered.
- Fallback: build the recorded backup clip SSOT §20 flags as "recommended but not yet built." This becomes a Phase 3 MUST, since Phase 3 is explicitly about demo reliability under judged conditions.
- **Optional multimodal beat (SSOT §20, §29 — IF TIME, sign-off-gated, NOT part of the locked demo above):** a candidate additional beat where a screenshot showing a contradicting metric reading is uploaded instead of/alongside the mocked telemetry value, reusing the exact same intervention/confirmation mechanism. This does **not** alter the two locked beats above and must not be substituted into the judged run without the team explicitly approving a change to the golden demo script — do not silently add a third reasoning beat, per SSOT §25 decision #11 and the existing escalation rule (ESCALATION section below).

### Reliability Hardening (demo-specific)
- Repeated demo runs: rehearse the golden path enough times that its timing is unremarkable (SSOT §19's stated bar).
- Failure scenarios / cold-start / network failure / unexpected input / multi-speaker / interruption / recovery: re-run each Phase 2 adversarial test specifically *inside* the golden-demo context (not just in isolation) to catch interaction effects.
- Fallback behavior: confirm the recorded backup clip plays cleanly as a standalone fallback if live ASR/interruption fails during judging.

### Hackathon Readiness
- Final judging alignment: SSOT §19 table maps every judging criterion to specific technical/demo evidence — verify each row actually has evidence in hand (test logs, metrics, recorded clip) before judging, not just a design claim.
- Technical questioning prep: the `AI interpretation ≠ deterministic authorization ≠ human authorization ≠ execution` boundary (§3 above) should be stated near-verbatim, per SSOT §7.
- Architecture explanation: SSOT §4–9, unchanged.
- Evaluation evidence: Phase 2's measured metrics, presented honestly including known limitations (mocked telemetry, small evaluation set, hackathon-scoped topology).
- Agora integration explanation: SSOT §10's verified-vs-assumed distinctions — do not overclaim anything marked ASSUMED as VERIFIED when presenting.
- "Why this is not just another AI chatbot": SSOT §3's core-insight and §5.4's differentiation (unsolicited-intervention system + reality-grounding, not conversational-consistency alone).
- Limitations/future work: state honestly — no real third-party integration, no production architecture, small evaluation set, fixed 4-metric telemetry, hackathon-scoped topology (~8–12 nodes).

### Testing (Phase 3)
Full demo rehearsal runs (repeated, timed), fallback-clip playback test, judging Q&A dry run against SSOT §19's evidence table.

### Acceptance Criteria (Phase 3)
- Golden demo runs reliably enough that timing feels unremarkable across at least several consecutive rehearsals.
- Recorded fallback clip exists and plays cleanly.
- Every row in SSOT §19's judging-alignment table has concrete evidence in hand.
- UI shows facts/hypotheses/decisions/timeline/risk/interventions correctly reflecting the State Store during a live run.

### Phase 3 Completion Gate
The team can run the golden demo live, with a working fallback, and answer the SSOT §19 judging questions with evidence already collected — not promises.

---

## 7. Complete Dependency Map

| Dependency | Purpose | Phase introduced | Verification status |
|---|---|---|---|
| Agora Conversational AI Engine (RTC+RTM+`speak`) | voice layer | 1 | Enabled on project; live behavior NEEDS TESTING (spike) |
| `agora-agents` SDK (Python/TS/Go) | recommended integration path over hand-rolled REST | 1 | VERIFIED as recommended path |
| Agora Customer ID/Secret | Basic Auth for `/join`,`/speak` | 1 | Existence in Console UNCONFIRMED — check first |
| `networkx` (Python) | topology graph + BFS/DFS | 1 | Not yet installed — planning-only |
| LLM provider (unspecified) | structured extraction | 1 | NOT YET DECIDED — open implementation choice, Antigravity may choose within budget/latency constraints, flag choice for team awareness |
| SQLite | State Store persistence | 1 | Decided in principle |
| `mock_telemetry.py` | 4 fixed metrics | 2 | Not yet built, deliberately deferred to Phase 2 |
| Recorded backup clip | demo fallback | 3 | Not yet built |

No new dependency beyond this table may be introduced without flagging it — see Autonomy/Escalation in the final prompt.

---

## 8. Complete Configuration Requirements

| Variable | Purpose | Required/Optional | Public/Private | Consumed by |
|---|---|---|---|---|
| `AGORA_APP_ID` | Agora project identity | Required | Public (non-secret) | RTC client, backend |
| `AGORA_CUSTOMER_ID` | REST Basic Auth | Required (pending Console confirmation) | Private | Backend `/join`,`/speak` calls |
| `AGORA_CUSTOMER_SECRET` | REST Basic Auth | Required (pending Console confirmation) | Private, secret | Backend `/join`,`/speak` calls |
| `LLM_PROVIDER_API_KEY` | extraction service | Required | Private, secret | Extraction service only |
| `AGORA_CHANNEL_NAME` | demo channel | Required | Public | RTC client, backend |
| `AGENT_METRICS_ENABLED` | latency instrumentation | Required (Phase 1) | Public | Agora `/join` payload |

No secrets are ever placed in frontend code or logs. Backend-only secrets stay backend-only, per skill §15/§17.

---

## 9. Complete Risk Register

Reproduced and phase-tagged from SSOT §23 (unchanged in substance):

| Risk | Phase | Fallback |
|---|---|---|
| Multi-speaker UID misattribution | 1 (spike) | Manual UID tagging workaround if native attribution fails |
| `speak(INTERRUPT)` doesn't reliably cut through live speech | 1 (spike) | Reframe pitch to "delivers immediately, can in principle be talked over"; explore `interruptable:false` custom LLM path only if needed |
| Agent talks over humans despite silence attempt | 1 (spike) | Fall back to bypassing agent's `llm` slot entirely |
| Latency exceeds ~1–2s budget | 1→2 | Identify slow stage via `AGENT_METRICS`; narrow demo scenario if unresolved |
| Customer ID/Secret not provisioned | 1 | Provision before implementation continues — check first, don't assume |
| ASR/TTS/LLM vendor undecided | 1 | Antigravity decides within constraints, flags choice |
| Live demo fragility | 3 | Rehearsal + recorded backup clip |
| Time constraint (hard 4 Sep deadline) | all | Strict MUST/SHOULD/IF-TIME/CUT discipline (SSOT §21); flag rather than silently cut scope |

---

## 10. Verified / Assumed / Needs Testing

Carried forward from SSOT §24 verbatim in substance — do not treat anything in ASSUMED or NEEDS TESTING as settled until Phase 1's spike produces evidence:

- **VERIFIED:** RTM transcript path; `remote_rtc_uids:["*"]` schema; transcript event schema; `speak` payload schema (`INTERRUPT`/`APPEND`); `interrupt_mode` schema; `AGENT_METRICS` config path/event code; Customer ID+Secret as the auth mechanism (not App ID+Certificate); project features enabled; `agora-agents` SDK as recommended path.
- **ASSUMED:** `priority: IGNORE` as a third enum value; `start_of_speech.mode:"manual"` achieving silence-by-default; Customer ID/Secret pair actually existing in Console.
- **NEEDS TESTING (all resolved by Phase 1's spike, not before):** multi-human UID attribution under overlapping speech; acoustic behavior of `INTERRUPT` mid-human-speech; whether the silence mechanism actually works; real end-to-end latency; actual `AGENT_METRICS` payload shape.

---

## 11. Final Architecture Freeze

Unchanged from SSOT §4–9 and §25. This blueprint adds only implementation-level specificity (data contracts, state schema fields, sequencing detail) strictly within that frozen design. Nothing in this blueprint changes a LOCKED decision. Any future need to change one is a **TECHNICAL BLOCKER**, to be reported per the skill's escalation format, not silently implemented.

---

## 12. Final Definition of Done

The AEGIS hackathon prototype is done when:

1. The golden demo (SSOT §20) runs live, reliably, producing both killer demo moments, with a working recorded fallback.
2. Every SSOT §19 judging-alignment claim has concrete evidence (test logs, measured metrics, or the demo itself).
3. AEGIS never, in any tested path, executes/blocks/overrides an action or treats an unconfirmed claim as fact.
4. All five Phase-1 Critical Unknowns are resolved (PASS or documented failure mode) and reflected honestly in the team's presentation.
5. Phase 2's measured accuracy/reliability numbers are the numbers actually presented to judges — no invented statistics.

---

---

# MASTER ANTIGRAVITY IMPLEMENTATION PROMPT

*Copy everything below this line directly into Antigravity.*

---

## CONTEXT

You are implementing **AEGIS**, a voice-native AI Incident Commander, for the EchoSphere: Agora Conversational AI Hackathon (PS4 — Voice AI Incident Commander), team AI Slayers. Strategy and architecture are **LOCKED**. Zero application code currently exists. Full context is in `AEGIS_SSOT.md` (authoritative reference — read it before starting) and this blueprint. Agora project `Xo4FYDFSa` / App ID `1dbe4a73adb1480086fedd84493f92a7` has `rtc`, `rtm`, `convoai` enabled and confirmed ready. Hard deadline: 4 Sep 2026, 11:59 PM IST.

## APPROVED PRODUCT

A voice-native AI participant joins a live incident call, builds a trustworthy shared operational state from the conversation (facts, hypotheses, decisions, proposed actions), evaluates every proposed action against a deterministic risk engine (staleness, decision-reversal, topology blast-radius, and — Phase 2 — telemetry/visual evidence grounding), and barges in over live audio to require explicit human confirmation before treating any action as authorized. It never executes, blocks, or overrides anything itself.

## FROZEN ARCHITECTURE

```
Agora Voice (multi-party RTC, remote_rtc_uids: ["*"])
   → RTM transcript delivery
   → LLM Structured Extraction (independent backend service)
   → Incident State Store (facts, hypotheses+staleness, decisions, ownership, timeline, proposed actions, evidence)
   → risk_engine.evaluate(proposed_action, state, topology, evidence[])  — ONE function only, PURE (returns a verdict; never writes to the State Store — the application layer applies the result)
   → Intervention Governor (SILENT→SUGGEST→ASK→WARN, ≤1 speak/45s, hard limit, no exceptions)
   → Agora speak (priority: INTERRUPT) if WARN/ASK
   → Human spoken confirm/deny/hold, captured via the SAME extraction pipeline
   → State Store updated; proposed action resolved
```

Boundary to preserve everywhere: **AI interpretation ≠ deterministic authorization ≠ human authorization ≠ execution.**

Do not collapse the three-plus risk-engine checks into fewer. Do not split them into separate "engines." Do not add a fourth pipeline for evidence (telemetry or visual) — it is an additional input to the same `risk_engine.evaluate()` call, generalized in Phase 2 (SSOT §29). `risk_engine.evaluate()` must never write to the State Store directly, under any circumstance — it returns a `RiskVerdict` (and, where applicable, a staleness determination); the State Store/application layer is what applies any resulting state change.

## CURRENT REPOSITORY/ENVIRONMENT

Clean workspace, no code. Inspect the local Agora Skill at `.agents/skills/agora/` (`SKILL.md`, `references/conversational-ai/README.md`, `architecture.md`, `agent-toolkit.md`) and the Agora MCP (`https://mcp.agora.io`) before writing any Agora integration code — both are available in your environment. Prefer, in order: official Agora docs → installed Agora Skill → Agora MCP → existing verified project config → existing working implementation. Do not rely on remembered Agora API shapes when current documentation is reachable.

**Before any REST call to `/join` or `/speak`:** confirm whether an Agora Customer ID/Secret pair exists in Console (Developer Toolkit → RESTful API). This is unconfirmed and is the one candidate hard blocker in the project. If missing, provision it before proceeding; if you cannot provision it yourself, STOP and report — do not build around its absence.

## PHASE 1 REQUIREMENTS — MVP / Core System

Goal: a complete, live, functional AEGIS loop, end to end. Not polished; not yet rigorously evaluated.

Execute in this exact order (dependency + risk + validation value, per the frozen build order — do not reorder):

1. **Agora real-time plumbing spike** (Track A, live-voice-dependent). Verify, in this order: (a) Customer ID/Secret exist; (b) two humans join distinct RTC UIDs, agent joins with `remote_rtc_uids:["*"]`, RTM enabled per the exact join-payload flags documented in the Agora Skill/docs; (c) transcript UID attribution correctness under overlapping speech; (d) silence-by-default — try `turn_detection.config.start_of_speech.mode:"manual"` first; if it fails to suppress unsolicited turns through ordinary conversation, fall back to bypassing the agent's built-in `llm` slot; (e) acoustic behavior of `speak(priority:INTERRUPT)` both in silence and while a human is actively speaking; (f) `AGENT_METRICS` payload shape and real per-stage/end-to-end latency across multiple calls. Record each result as PASS or a specific documented failure mode — never leave a result ambiguous.
2. **In parallel, starting immediately (Track B: isolated construction, no dependency on or assumption about live Agora behavior):** build and unit-test `risk_engine.evaluate()` standalone — a real `networkx` graph (~8–12 nodes, typed edges `depends_on`/`reads_schema`/`compatible_with`), with staleness, decision-reversal, and topology blast-radius (BFS/DFS) checks. Output contract: `{risk_tier: LOW|MEDIUM|HIGH, reasons: string[]}`, where `reasons` names the specific rule/node/edge violated on any non-LOW verdict. `risk_engine.evaluate()` is a pure function — it returns this verdict, it never writes to the State Store.
3. Build and independently test the Incident State Store (SQLite-backed, in-memory read path acceptable) with the entity schema: facts (no staleness), hypotheses (`reinforcement_count`, `status: active|stale` — the risk-evaluation logic returns a staleness determination as its output; the State Store/application layer applies it to set this field, since `risk_engine.evaluate()` itself never writes to the Store), decisions (append-only ledger), proposed actions (`status: pending|confirmed|declined|held`, holds the resulting `RiskVerdict`), and an append-only timeline. Do not collapse these four claim types into one.
4. Build the LLM Structured Extraction service. Contract: transcript event in (`uid`,`turn_id`,`role`,`text`,`final`) → `ExtractedClaim[]` out, with fields `claim_id`,`type`(`fact|hypothesis|decision|proposed_action|confirmation|override|hold|none`),`text`,`speaker_uid`,`timestamp`,`source_turn_id`,`target_ref`(required for `proposed_action`),`metric_ref`(optional),`ownership_tag`(optional). Reject and log (never crash) malformed output; on any extraction failure after one bounded retry, emit `type:"none"` rather than blocking the pipeline. Test this offline against a written fixed transcript you author, covering every claim type, before any live audio exists.
5. Build the Intervention Governor as an explicit state machine `SILENT→SUGGEST→ASK→WARN`, hard rate-limited to ≤1 spoken intervention per ~45 seconds with **no exceptions** — a second HIGH-risk event inside the window is re-evaluated against current state (not spoken stale) once the window reopens, never dropped silently. Test against synthetic, hand-authored `RiskVerdict` objects.
6. Wire steps 2–5 into a text-only end-to-end pipeline (fixed transcript → extractor → State Store → risk engine → Governor → console output, no audio). Checkpoint: this must correctly reproduce the golden-demo beats (SSOT §20) on text input, including both killer-moment verdicts, before proceeding.
7. **Gate:** only after step 1 passes (all unknowns resolved to PASS or documented failure), wire Track A's live transcript events into the already-tested Track B pipeline, and wire Governor output into the real `speak` call. Implement human-confirmation capture: when a `proposed_action` is `pending`, the next classified `confirmation`/`override`/`hold` claim resolves it; if no clear resolution arrives within a bounded window, the action stays `pending` — never auto-authorize on silence or ambiguity.
8. Build a minimal Core UI showing the live State Store snapshot. Add basic structured logging of every pipeline stage (transcript received, claim extracted/rejected, state mutation, risk verdict, governor decision, `speak` call, human resolution) — this log format is reused as Phase 2's evaluation input, so build it properly the first time.
9. Run a first live pass of the golden demo (SSOT §20) end to end.

**Phase 1 completion gate:** the full loop runs live at least once end to end. Note: beat 3 of the golden demo (telemetry contradiction) depends on `mock_telemetry.py`, which is a Phase 2 deliverable — it will not yet fire in Phase 1's first live pass. For Phase 1, verify the non-telemetry half of the loop instead: staleness detection, decision-reversal detection, and the topology blast-radius check (the core of beat 6) all firing correctly live. Treat full beat-3-into-beat-6 compounding as a Phase 2 acceptance item once telemetry grounding is wired in. No action is ever auto-authorized without an explicit human claim.

## PHASE 2 REQUIREMENTS — Intelligence / Reliability / Evaluation

1. Build `mock_telemetry.py` serving exactly 4 fixed metrics (`pool_utilization`,`error_rate`,`p99_latency`,`schema_version`) and wire it as an additional input to the *same* `risk_engine.evaluate()` function — do not create a new pipeline or a fourth "engine." This is what completes golden-demo beat 3.
2. Build an evaluation harness that replays the Phase 1 fixed transcript plus new adversarial transcripts (ambiguous hedges, reversed decisions without new evidence, near-boundary blast-radius cases, a correctly-reinforced-not-stale hypothesis, a genuinely LOW-risk proposed action) through the text-only pipeline, diffing actual vs. hand-labeled-expected claims/verdicts.
3. Measure and record (do not estimate): extraction accuracy, attribution accuracy (from repeated live adversarial multi-speaker tests), risk detection precision/recall, false-positive and false-negative intervention rates, decision-reversal detection accuracy, real end-to-end latency distribution (reusing `AGENT_METRICS` + app timestamps across many calls), intervention latency. Adjust risk-tier thresholds based on the first measurement pass — do not touch the architecture to fix an accuracy problem.
4. Deliberately trigger and verify correct (non-crashing, non-silently-wrong) behavior for: invalid/timeout LLM output, duplicate transcript events, out-of-order events, concurrent/overlapping speech, at least one disconnect/reconnect cycle mid-incident, two proposed actions on the same `target_ref` arriving close together, and a missing/unreachable telemetry endpoint (must be treated as "not evaluated," never silently treated as clean/LOW-risk).
5. Re-run the Phase 1 regression suite after every change in this phase; a flipped previously-correct verdict is a regression to fix, not a result to accept.

**Phase 2 completion gate:** every accuracy/reliability number the team plans to present to judges has actually been measured and logged, not estimated.

## PHASE 3 REQUIREMENTS — Polish / Demo / Hackathon Readiness

1. Build UI views for: timeline, fact/hypothesis list with confidence/staleness indicators, decision ledger, proposed-action list with risk visualization tied to `RiskVerdict.reasons`, intervention history, and a final incident summary — all reading from the existing State Store schema. Do not add new state fields to satisfy the UI; if a field is missing, that is a Phase 1/2 contract gap, fix it there.
2. Rehearse the exact golden demo (SSOT §20, beats 1–9) repeatedly until timing is unremarkable. Do not reorder or reword the beats — the beat-3-into-beat-6 compounding sequence is the load-bearing "killer demo moment" for judging.
3. Build the recorded backup demo clip (flagged in SSOT as recommended but not yet built) as a fallback for live ASR/interruption failure during judging.
4. Re-run each Phase 2 adversarial reliability test specifically inside the golden-demo context (not just isolated) to catch interaction effects.
5. Prepare judging-readiness material mapped 1:1 to the SSOT §19 evidence table — every row must have concrete evidence (a test log, a measured metric, or the demo itself) in hand, not a design claim.

**Phase 3 completion gate:** the team can run the golden demo live with a working fallback and answer judging questions with evidence already collected.

## COMPONENT SPECIFICATIONS

See "Component Specifications" and each numbered component in Section 4 of this blueprint (above the prompt) for full responsibility/input/output/state/must-not tables for: Agora RTC/RTM/TTS layer, LLM Extraction service, Incident State Store, `risk_engine.evaluate()`, Intervention Governor, `speak` delivery, Human Confirmation capture, Core UI, Observability, and (Phase 2+, SSOT §29) Multimodal Evidence Ingestion.

## DATA CONTRACTS

- `ExtractedClaim`: see Phase 1 step 4 field list above — this is the finalized schema; implement it exactly, do not invent additional required fields.
- `RiskVerdict`: `{risk_tier: LOW|MEDIUM|HIGH, reasons: string[]}` — `reasons` non-empty on any non-LOW verdict.
- Transcript event: `{uid, turn_id, role, text, final}` — Agora-verified schema, do not modify.
- `speak` request: `{text (≤512 bytes), priority: INTERRUPT|APPEND, interruptable: bool}` — do not rely on `priority:IGNORE` for anything load-bearing; it is unverified against official docs.
- Incident State entities: facts, hypotheses (`reinforcement_count`,`status`), decisions, proposed_actions (`status`,`risk_verdict`,`resolved_by_uid`,`resolved_at`), timeline — see Phase 1 step 3 above for exact fields.
- `Evidence` (Phase 2+, SSOT §29): `{evidence_id, source_type: telemetry|visual, metric_name, value, unit, extraction_certainty: high|low, source: mock_telemetry|screenshot_upload, uploader_uid, timestamp, target_ref, raw_reference}` — new entity, distinct from `ExtractedClaim`; do not collapse the two. `risk_engine.evaluate()` signature generalizes to accept `evidence[]` (superset of telemetry- and visual-sourced entries) in place of the original `telemetry` parameter.

## AI CONTRACTS

The LLM only converts speech into typed claims (`ExtractedClaim`). It never outputs a risk tier, never decides whether to intervene, and its output is never treated as confirmed fact until it passes through the State Store and (for proposed actions) the risk engine. All risk/safety decisions are made exclusively by `risk_engine.evaluate()`, in Python, deterministically, as a pure function: it returns a `RiskVerdict` (and, where staleness is being determined, a return value the State Store applies); it never writes to the State Store itself, and no document should be read as implying otherwise. Human authorization is required for any proposed action before it is treated as authorized — silence or ambiguity is never treated as authorization. **The same boundary applies to the extraction service's vision-mode code path (SSOT §29, Phase 2+):** it only converts a screenshot into structured `Evidence` (a metric+value reading), never a risk tier or an intervention decision; `extraction_certainty` is a categorical high/low flag describing extraction quality, not a probabilistic belief score — it feeds a deterministic branch in `risk_engine.evaluate()`, it does not decide anything itself.

## AGORA INTEGRATION

Use the `agora-agents` SDK (recommended path) over hand-rolled REST where practical. Verify every API shape (join payload, RTM flags, `speak` schema, `AGENT_METRICS` event code 111 payload, auth mechanism) against official docs / the local Agora Skill / Agora MCP before use — do not assume the SSOT's documented shapes are complete; they include explicit ASSUMED/NEEDS TESTING flags (Section 10 of this blueprint) that you must resolve empirically in the Phase 1 spike, not assume.

## EXTERNAL INTEGRATIONS

`networkx` (topology graph), an LLM provider for extraction (not yet chosen — you may choose one within reasonable cost/latency for a hackathon demo; flag your choice explicitly in your status report so the team is aware; **if Phase 2's multimodal extension is built, confirm this provider also supports vision input — flag rather than silently substitute a different provider**), SQLite (State Store persistence), `mock_telemetry.py` (Phase 2, local mock only). No real third-party integrations (Jira/Slack/PagerDuty) — this is explicitly out of scope, do not add any.

## CONFIGURATION

`AGORA_APP_ID` (public), `AGORA_CUSTOMER_ID`/`AGORA_CUSTOMER_SECRET` (private, pending Console confirmation), `LLM_PROVIDER_API_KEY` (private), `AGORA_CHANNEL_NAME` (public), `AGENT_METRICS_ENABLED` (public). Never place a secret in frontend code, client bundles, or logs.

## ERROR HANDLING

Invalid/malformed extraction output → validate, reject, log, never crash the pipeline; downstream, treat as `type:"none"`. LLM API timeout/failure → one bounded retry, then `type:"none"`. Duplicate `claim_id` → idempotent no-op. Out-of-order events → sort by timestamp on read. Missing telemetry/evidence (including an unreadable or malformed screenshot, SSOT §29) → treat as "not evaluated" in the risk engine, never as evidence of LOW risk. Governor rate-limit window collision → queue and re-evaluate against current state when the window reopens, never drop silently and never speak stale content. No response to a pending proposed action → stays `pending` indefinitely (or until a bounded demo-scoped timeout you choose, still resulting in "not authorized," never "authorized").

## SECURITY

Backend holds all secrets (Customer Secret, LLM API key); nothing sensitive reaches the frontend. Client/server boundary: the frontend only ever displays State Store snapshots and sends nothing that could itself authorize an action — authorization only happens through the voice pipeline's classified human claims.

## OBSERVABILITY

Structured logs for every pipeline stage from Phase 1 onward (see Phase 1 step 8); this log format is the input to Phase 2's evaluation harness, so define it once, correctly, and reuse it.

## TESTING

Phase 1: risk-engine unit tests (each of the 3 checks, at least one HIGH-triggering case each), Governor unit tests, State Store mutation/idempotency tests, extractor offline test against the fixed transcript, spike verification steps, text-only and then live golden-demo passes. Phase 2: adversarial transcript regression suite, live adversarial multi-speaker tests, each reliability failure mode deliberately triggered once. Phase 3: repeated golden-demo rehearsals, fallback-clip playback, judging Q&A dry run.

## EVALUATION

Phase 2 metrics and datasets exactly as specified in Section 5 ("Evaluation infrastructure") of this blueprint, including the multimodal evaluation additions (SSOT §29) if that capability is built — measure, don't estimate; report honestly including the evaluation set's small hackathon-appropriate size.

## DEMO REQUIREMENTS

Exactly SSOT §20's golden demo, beats 1–9, unmodified in sequence or wording intent. The killer-demo-moment compounding of beat 3 into beat 6 is load-bearing; do not reorder. The optional multimodal beat (SSOT §20, §29) is a separate, sign-off-gated, IF-TIME addition — build and rehearse it independently if time allows, but never substitute it into the locked judged run without explicit team approval.

## SCOPE / NON-GOALS

**MUST BUILD:** multi-speaker ASR→extraction, State Store with staleness+ledger, real topology graph+BFS traversal, Governor wired to `speak`, one rehearsed golden-path scenario end to end.
**SHOULD BUILD:** telemetry-grounding tool call, conflict detection over the fixed claim schema, on-demand spoken status summary, live timeline visualization, multimodal evidence ingestion (text + screenshot side-channel) and vision-mode extraction generalizing `risk_engine.evaluate()`'s evidence input (SSOT §29).
**IF TIME:** proactive rate-limited nudges, auto-generated final summary, task-ownership UI treatment, evidence UI treatment and the optional sign-off-gated multimodal demo beat (SSOT §20, §29).
**DO NOT BUILD:** any real Jira/Slack/PagerDuty integration; speaker diarization ML; more than one `risk_engine.evaluate()` function or any separately-branded "engine"; open-domain contradiction detection beyond the fixed claim schema; Bayesian/probabilistic confidence modeling (including for visual evidence — `extraction_certainty` is categorical, never a probability score); multi-agent personas/voices; any path where AEGIS executes a proposed action itself; production/scalability architecture; open-domain image understanding beyond named-metric dashboard-style readings; a second AI/vision "engine" or agent.

## ACCEPTANCE CRITERIA

As specified per-phase in Sections 4, 5, and 6 of this blueprint above. In summary: Phase 1 = live functional loop; Phase 2 = measured accuracy/reliability evidence; Phase 3 = rehearsed, evidenced, fallback-protected demo.

## AUTONOMY

You may: inspect the repository and environment; inspect installed Skills; use the Agora MCP; consult official Agora documentation; choose the LLM provider, exact file/folder layout, variable/function names, minor implementation details, small refactors, equivalent low-level approaches, and ordinary bug fixes; choose which silence-mechanism candidate to try first (manual mode first, per SSOT).

## ESCALATION

You must NOT independently: redesign the product; change the core architecture (§3/frozen architecture above); change any critical data contract without flagging it first; replace a major dependency; change a safety boundary (never-execute, human-authorization-required, ≤1/45s rate limit); change the core workflow; add features outside the MUST/SHOULD/IF-TIME lists; or silently work around an architectural constraint. If you encounter a problem that seems to require any of the above — for example, if `speak(INTERRUPT)` genuinely cannot cut through live human speech, or the silence-by-default mechanism cannot be made to work by either candidate method — **STOP and report**: name the affected decision, the evidence, why it's a problem, the smallest viable correction, and the implementation impact. Do not silently redesign around it.

---

Inspect the existing codebase and environment before modifying anything. Implement this task within the approved architecture. Do not redesign the product or core architecture. Preserve existing contracts unless explicitly instructed otherwise. Verify external APIs and SDK behavior against the available authoritative documentation, Skills, or MCP. Run the relevant tests and report exactly what was implemented, what was verified, and any unresolved issues.
