# AEGIS — MULTIMODAL INCIDENT INPUT: CONTROLLED REVISION

*This document performs a controlled extension of the frozen AEGIS design to add multimodal (voice + text + image) incident evidence. It does not reopen strategy, redesign Agora, or touch deployment. Full revised source documents follow as separate files: `AEGIS_SSOT_v2_MULTIMODAL.md` and `AEGIS_MASTER_IMPLEMENTATION_BLUEPRINT_v2_MULTIMODAL.md`. Both are the originals with only the changes below applied — no unrelated section was rewritten.*

---

# CHANGE IMPACT REPORT

| # | Existing concept | Required change | Reason | Phase affected | Technical impact |
|---|---|---|---|---|---|
| 1 | SSOT §1 "Core solution" / §2 "Core product workflow" | Wording extended to note text/screenshot evidence alongside speech | The product description should not silently become inaccurate once this capability exists | N/A (description only) | None — no architecture change, wording only |
| 2 | SSOT §4 Frozen Architecture diagram | One line generalized: "telemetry contradiction check" → "telemetry/visual evidence contradiction check"; pointer added to new §29 | The risk engine's existing telemetry-input slot is being broadened to a second evidence source, not replaced with a new pipeline | 2 | Data-contract generalization only — see #7 below |
| 3 | SSOT §5 Component Pipeline | Two new components appended (Multimodal Evidence Ingestion; vision-mode extension to the existing Extraction service) | A new, minimal ingestion path is architecturally necessary — there is currently no non-voice input channel at all | 1 (schema only), 2 (behavior) | New component, but reuses existing extraction service and reliability pattern; no new "engine" |
| 4 | SSOT §6 Component Specifications table | New row for Multimodal Evidence Ingestion; `risk_engine.evaluate()` row's Inputs cell generalized from "(+ telemetry, Phase 2)" to "(+ evidence: telemetry/visual, Phase 2+)" | Table must reflect the generalized input | 2 | Same as #2/#3 |
| 5 | SSOT §7 AI Architecture | One clause added noting the LLM's vision-mode extraction is still "understands only," never a risk decision | Preserve the `AI interpretation ≠ deterministic authorization` boundary explicitly for the new modality | 2 | None — boundary already covers this, just stated explicitly |
| 6 | SSOT §8 State Model | New `Evidence` entity row added to the table; explicit note that it is NOT collapsed into `Hypothesis` or `Fact` | Preserve the fact/hypothesis/decision/proposed-action distinction the original request required; evidence is a materially different kind of thing (an observation about reality, not a human assertion) | 1 (schema), 2 (behavior) | New State Store collection |
| 7 | SSOT §9 Data Contracts | New `Extracted Evidence` contract row; `Extracted claim` row gets an optional `source_modality` field noted | Text input reuses the transcript-event contract as-is; evidence needs its own contract, distinct from claims | 1 (schema), 2 (behavior) | New data contract (`Evidence`); one optional field added to an existing one |
| 8 | SSOT §12 External Dependencies | Note added: confirm the chosen LLM provider supports vision input; if not, this is the one new dependency this extension could introduce | Cannot assume the not-yet-chosen extraction provider is multimodal-capable | 2 | Possible new dependency — flagged, not assumed |
| 9 | SSOT §17 Tools & Actions | New row: screenshot/text evidence ingestion, explicitly non-destructive, no confirmation required for ingestion itself (only for any resulting proposed action) | Consistency with the existing tools table's format and safety framing | 2 | None — same safety model as existing tools |
| 10 | SSOT §20 Final Demo | New subsection added describing an optional, IF-TIME, sign-off-gated additional demo beat — the locked 9-beat golden demo is NOT modified | SSOT §25 decision #11 locks the golden demo at two reasoning beats; adding a beat requires explicit escalation, not silent redesign | 3 | None to the locked demo; a clearly-flagged optional addition only |
| 11 | SSOT §21 Implementation Priorities | New SHOULD-BUILD items (Phase 2) for text/image ingestion and evidence-generalized risk check; new IF-TIME item (Phase 3) for the optional demo beat and evidence UI | Places the feature correctly in the existing MUST/SHOULD/IF-TIME discipline rather than silently becoming a MUST | 1–3 | Scope classification only |
| 12 | SSOT §24 Verified/Assumed/Needs Testing | New ASSUMED and NEEDS TESTING / NEEDS CONFIRMING items added for the multimodal extension, kept separate from the five existing Agora Critical Unknowns | Prevents conflating the voice-pipeline's critical unknowns with this unrelated extension's own unknowns | 2 | None — tracking only |
| 13 | SSOT §25 Architectural Decisions table | One new LOCKED row added documenting this extension's decision (evidence generalizes telemetry input; `Evidence` is a distinct entity; `extraction_certainty` is categorical, not probabilistic) | Every other architectural decision in the document is tracked this way; this one should be too, for consistency and to make the "does not reopen anything" claim auditable | N/A | None — documentation only |
| 14 | SSOT §26 Non-Goals | One new non-goal added: no open-domain image understanding beyond named-metric dashboard readings | Locks the scope down explicitly, preventing this extension itself from becoming the next scope-creep vector | N/A | None — scope discipline only |
| 15 | SSOT — new §29 | New section added in full: complete multimodal extension specification (see body below) | Keeps all substantive new detail in one place, so the inline edits above stay minimal and pointer-based | 1–3 | See full section below |
| 16 | SSOT — Critical Unknowns / Final Project Status | One clarifying line added: this extension's own unknowns are tracked in §29 and do not alter or block the five existing Agora Critical Unknowns | Prevents the two unrelated unknown-lists from being conflated in status reporting | N/A | None |
| 17 | Blueprint §3 Frozen Architecture | Same one-line generalization as SSOT #2 | Consistency between the two documents | 2 | Same as #2 |
| 18 | Blueprint Phase 1 (§4) | Explicit addition: State Store schema includes the (initially unused) `evidence` collection and `ExtractedClaim.source_modality` field from the start | Avoids a later schema migration; zero behavioral change to the Phase 1 gate | 1 | Schema-only addition, no new Phase 1 acceptance criteria |
| 19 | Blueprint Phase 2 (§5) | New subsection: multimodal evidence ingestion, vision-mode extraction, generalized `risk_engine.evaluate()` signature, and the associated evaluation additions | This is where the capability is actually built and evaluated, alongside telemetry grounding (already a Phase 2 item, same reasoning) | 2 | New component + generalized function signature; no change to existing Phase 2 items |
| 20 | Blueprint Phase 3 (§6) | New subsection: evidence UI treatment; optional demo beat, explicitly marked sign-off-gated and not a replacement of the golden demo | Consistency with SSOT §20's new subsection | 3 | UI addition only; demo change is optional and gated |
| 21 | Blueprint "DATA CONTRACTS" (Antigravity prompt section) | `Evidence` contract added; `ExtractedClaim` contract's optional field noted; `risk_engine.evaluate()` signature updated | The copy-paste Antigravity prompt must reflect the same contracts as the narrative blueprint | 1–2 | Same as SSOT #6/#7 |
| 22 | Blueprint "SCOPE / NON-GOALS" (Antigravity prompt section) | New SHOULD-BUILD / IF-TIME lines added; DO-NOT-BUILD list gets the same open-domain-image-understanding exclusion as SSOT §26 | Antigravity's scope discipline must match the SSOT's | 1–3 | Scope classification only |
| 23 | Blueprint "AI CONTRACTS" (Antigravity prompt section) | One clause added: vision-mode extraction is still "understands only" | Same reasoning as SSOT #5 | 2 | None |

**Not changed, and why:** Agora integration section (voice transport, RTC/RTM/`speak` mechanics), the Intervention Governor's rate-limit state machine, the golden demo's locked 9 beats, the Incident State Store's four original claim-type collections (beyond one optional field), deployment (explicitly out of scope for this revision), and every LOCKED decision in SSOT §25 items 1–13. None of these required any change to correctly incorporate multimodal evidence — the extension slots into the risk engine's already-planned "additional input producer to the same function" design (SSOT §4, original wording) without disturbing anything upstream or downstream of that one function's input.

---

# UPDATED AEGIS ARCHITECTURAL DEFINITION

```
VOICE (Agora RTC, live)         TEXT (side-channel form)        IMAGE (side-channel upload)
        |                               |                                |
        v                               v                                v
   RTM transcript event          wrapped as a transcript-       LLM Extraction service,
   {uid,turn_id,role,text,final}  event-shaped object            VISION MODE (same service,
        |                         (same shape)                   new code path)
        |                               |                                |
        +---------------+---------------+                                |
                         v                                                v
              LLM Extraction service                              Evidence object
              TEXT MODE (unchanged)                          {evidence_id, source_type,
                         |                                     metric_name, value, unit,
                         v                                     extraction_certainty,
              ExtractedClaim                                   source, uploader_uid,
      {fact|hypothesis|decision|                                timestamp, target_ref,
       proposed_action|confirmation|                            raw_reference}
       override|hold|none,                                              |
       source_modality: voice|text, ...}                                |
                         |                                              |
                         +------------------+---------------------------+
                                            v
                              Incident State Store
                    (facts, hypotheses, decisions, proposed_actions,
                     evidence  <-- NEW collection, timeline)
                                            |
                                            v
                    risk_engine.evaluate(proposed_action, state,
                                          topology, evidence[])
                    -- SAME function, SAME checks (staleness,
                       decision-reversal, blast-radius,
                       evidence-contradiction [generalized from
                       "telemetry-contradiction"]) --
                                            |
                                            v
                              Intervention Governor
                       (SILENT/SUGGEST/ASK/WARN, ≤1/45s -- unchanged)
                                            |
                                            v
                    Agora speak(priority: INTERRUPT) if WARN/ASK
                    -- SAME mechanism, regardless of which evidence
                       source triggered the verdict --
                                            |
                                            v
                    Human spoken confirm/deny/hold (voice pipeline,
                    unchanged) -> State Store updated
```

**What materially changed:** one function signature (`risk_engine.evaluate()` gains an `evidence[]` parameter that generalizes the already-planned `telemetry` parameter), one new State Store collection (`evidence`), one new minimal ingestion component (text/image side-channel), and one code branch inside the existing Extraction service (vision mode). Everything downstream of evidence production — state storage, risk evaluation, intervention, confirmation — is the same pipeline, unmodified, per the requirement that multimodal evidence become "usable evidence inside the existing reasoning architecture," not a parallel system.

**What did not change:** Agora's role and transport, the Governor, the confirmation pipeline, the golden demo's locked beats, the never-execute safety boundary, the one-risk-engine-function rule, and every other frozen decision in SSOT §25.

---

# UPDATED IMPLEMENTATION PHASES

### Phase 1 (unchanged core gate; one schema-only addition)
No new runtime behavior. The only Phase 1 change: the Incident State Store's schema is defined from the start with the `evidence` collection (unused/empty) and `ExtractedClaim.source_modality` field, so Phase 2 doesn't require a schema migration. This does not add a new Phase 1 acceptance criterion and does not touch the voice-loop completion gate defined in the original blueprint.

### Phase 2 (SHOULD BUILD — same tier as telemetry grounding, same reasoning)
- Build the Multimodal Evidence Ingestion component (text side-channel + image upload side-channel), explicitly outside Agora's transport.
- Extend the LLM Extraction service with a vision-mode code path producing `Evidence` objects, using the same reliability pattern (reject malformed output, bounded retry, never crash) already specified for text mode.
- Generalize `risk_engine.evaluate()`'s signature to accept `evidence[]` (telemetry + visual), preserving the exact same three-plus-one check structure — no new check added, the existing telemetry-contradiction check's input source is broadened.
- Implement the deterministic `extraction_certainty: low` → ASK-not-WARN branch.
- Run the evaluation additions specified below.
- **Confirm the chosen LLM provider supports vision input before building** — if not, flag the provider gap rather than silently substituting a different provider.

### Phase 3 (IF TIME, sign-off-gated)
- UI: evidence thumbnail + extracted reading shown alongside telemetry in the risk visualization.
- Optional demo beat (visual-evidence contradiction) — built and rehearsed only as a secondary/bonus demonstration, never substituted into the locked golden-demo run without the team explicitly approving a change to the golden demo script (existing escalation rule, unchanged).

**Explicit non-destabilization statement:** at no point does this extension change the Phase 1 completion gate, the golden demo's locked beats, or the voice pipeline's critical-path latency budget (visual evidence submission is not itself time-pressured the way live speech is, so it does not compete with the sub-second intervention path).

---

# UPDATED DATA / STATE MODEL

Only the contracts that actually need modification are listed — everything else in SSOT §8/§9 is unchanged.

**New entity: `Evidence`**

| Field | Type | Required | Notes |
|---|---|---|---|
| `evidence_id` | string (UUID) | yes | |
| `source_type` | enum: `telemetry` \| `visual` | yes | |
| `metric_name` | string | yes | scoped to named-metric dashboard-style readings only |
| `value` | string or number | yes | |
| `unit` | string | optional | |
| `extraction_certainty` | enum: `high` \| `low` | yes for visual; always `high` for telemetry | categorical, not a probabilistic confidence score — see §29 for why this distinction matters against the existing NON-GOAL on Bayesian modeling |
| `source` | enum: `mock_telemetry` \| `screenshot_upload` | yes | |
| `uploader_uid` | string \| null | required for `screenshot_upload`, null for `mock_telemetry` | |
| `timestamp` | ISO-8601 | yes | |
| `target_ref` | string \| null | optional | same naming convention as `proposed_action.target_ref` |
| `raw_reference` | string \| null | optional | pointer to stored file, never raw bytes in state |

**Modified entity: `ExtractedClaim`** — one optional field added: `source_modality: voice | text`. No other field changes. Text-sourced claims otherwise use the identical schema and validation path as voice-sourced claims.

**Modified function: `risk_engine.evaluate()`** — signature generalizes from `(proposed_action, state, topology[, telemetry])` to `(proposed_action, state, topology, evidence[])`. `evidence[]` is a superset containing both telemetry-sourced and visual-sourced `Evidence` objects. Output contract (`RiskVerdict = {risk_tier, reasons[]}`) is unchanged.

**State Store** — one new collection: `evidence: Evidence[]`, appended to `timeline` alongside the existing four claim-type collections. No change to `facts`, `hypotheses`, `decisions`, or `proposed_actions` beyond the one optional `source_modality` field on the claims that populate them.

**Deliberately not added:** a stored "relationship to existing claims" field on `Evidence` (computed by the risk engine at evaluation time, not stored — avoids stale/circular links); a numeric confidence score (would risk reopening the Bayesian/probabilistic-modeling non-goal — a categorical `extraction_certainty` flag is used instead).

---

# UPDATED EVALUATION STANDARD

Additions to the existing evaluation framework (does not replace or restructure any existing metric from the prior evaluation work):

| Test case | What it probes | Correct behavior | Metric |
|---|---|---|---|
| Correct screenshot interpretation | Baseline vision-mode accuracy | Extracted `metric_name`+`value` matches the ground-truth reading | Exact Match rate against a hand-labeled screenshot set |
| Incorrect screenshot interpretation | Hallucination in vision mode | System does not report a value not actually present in the image | Vision-mode hallucination rate (analogous to the existing text-extraction hallucination metric) |
| Ambiguous screenshots | Cropped/multi-metric/unclear images | `extraction_certainty: low`, or no `Evidence` produced — never a confident wrong answer | Binary pass/fail per scenario |
| Conflicting voice vs. image evidence | Precedence rule | `risk_engine.evaluate()` favors the `Evidence` entry over the spoken hypothesis in `reasons` | Binary pass/fail against hand-authored conflict scenarios |
| Stale visual evidence | Recency handling | Older evidence correctly outweighed by newer evidence of either source | **NEEDS BASELINE** — reuse the existing hypothesis-staleness timestamp approach; exact rule to be set from the first real test, not invented here |
| Irrelevant images | Off-topic upload | Zero `Evidence` objects produced | Binary pass/fail |
| Low-confidence visual evidence | Deterministic ASK-not-WARN branch | Governor never emits WARN on `extraction_certainty: low` evidence alone | Binary pass/fail (this is deterministic logic — should be 100%, not a target with tolerance) |
| Multiple evidence sources (telemetry vs. screenshot disagreeing with each other) | Cross-source conflict | **NEEDS DESIGN** — not resolved by this extension; flagged as an open question for the team, not silently decided | N/A until designed |
| Visual evidence contradicting a human claim | The primary demo scenario | Full pipeline fires correctly end to end (extraction → state → risk → Governor → `speak` → confirmation) | This scenario should have the best evaluation coverage of the set, since it is the one the optional demo beat depends on |

**Status labels applied, per the request:**
- **VERIFIED:** nothing — no multimodal code exists yet.
- **ASSUMED:** the chosen LLM provider's vision mode can reliably read a named metric off a typical dashboard screenshot.
- **NEEDS TESTING:** extraction accuracy across dashboard styles/resolutions; whether `extraction_certainty` actually correlates with correctness; malformed/non-dashboard image handling; vision-mode extraction latency (off the voice critical path, so not gated by the ~1–2s intervention budget).
- **NEEDS CONFIRMING:** whether the extraction LLM provider (still "NOT YET DECIDED" per the original SSOT) supports vision input at all.

---

# UPDATED DEMO FLOW

The locked golden demo (SSOT §20, beats 1–9) is **unchanged**. This section defines only the optional, sign-off-gated addition:

**Candidate additional beat (Phase 3, IF TIME, requires explicit team approval before inclusion in the judged run):**

| Beat | Speaker/Actor | Line/Action | Proves |
|---|---|---|---|
| A | Eng A | "Pool utilization looks fine, like 40%." (same line as golden-demo beat 2 — reused, not duplicated, if this beat is shown as an alternate path rather than an addition) | `hypothesis` claim |
| B | Eng B | Uploads a screenshot of a cloud dashboard showing 91% pool utilization | `Evidence` object, `source_type: visual`, extracted via the vision-mode extraction path |
| C | (system) | `risk_engine.evaluate()` flags the contradiction between the spoken hypothesis and the visual evidence | Same evidence-contradiction check that already handles the mocked-telemetry case, now firing on a second evidence source |
| D | **AEGIS** | "Hold — the screenshot shows pool utilization at 91%, not 40%. Want to re-check before ruling it out?" | Reuses the exact same intervention mechanism (`speak(priority:INTERRUPT)`) as the golden demo's beat 3 — nothing new on the delivery side |
| E | Eng B | Confirms/holds per the existing confirmation pipeline | Unchanged human-confirmation mechanism |

**Why this stays optional rather than becoming a third locked beat:** SSOT §25 decision #11 locks the golden demo at exactly two reasoning beats, chosen deliberately as "more convincing to judges than one capability shown twice." Adding a third beat is a change to that locked decision, not an execution detail — per the existing escalation rule, this requires the team's explicit sign-off, not this document's unilateral inclusion. If approved, the team should decide whether it replaces beat 2/3's telemetry version, runs as an alternate rehearsed path, or is dropped from the judged run entirely and shown only if asked "does it work with images too?" during Q&A.

---

*Full revised source documents follow: `AEGIS_SSOT_v2_MULTIMODAL.md` and `AEGIS_MASTER_IMPLEMENTATION_BLUEPRINT_v2_MULTIMODAL.md`. Each is the original document with exactly the changes itemized in the Change Impact Report above applied — no unrelated section was rewritten, reworded, or reordered.*
