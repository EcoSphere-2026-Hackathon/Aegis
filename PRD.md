# AEGIS — Product Requirements Document

*Authoritative sources: `AEGIS_SSOT.md` (product/architecture truth), `AEGIS_MASTER_IMPLEMENTATION_BLUEPRINT.md` (build plan), `AEGIS_QUALITY_BENCHMARK_ACCEPTANCE_STANDARD.md` (acceptance bar). This document sits above all three as the stable product definition — it does not redesign, add features, or invent requirements beyond what those documents establish.*

---

## 1. Product Overview

- **Product name:** AEGIS
- **Hackathon / track:** EchoSphere: Agora Conversational AI Hackathon (organized by KNOTiC) — Applied AI / AI Engineering track, Problem Statement PS4: *Voice AI Incident Commander* (locked; the hackathon rules do not permit inventing a new problem statement)
- **One-line description:** A voice-native AI participant that joins a live incident call, builds a trustworthy shared operational state from the conversation, and intervenes via real-time voice to demand human confirmation before any consequential action is treated as authorized.
- **Product category:** Voice AI Incident Commander — an unsolicited-intervention safety layer for live incident calls, not a notetaker and not a conversational chatbot.
- **Target users:** SRE / incident-response teams running live P1/P2 incidents.

---

## 2. Problem Statement

During a live P1 incident, teams rarely fail from missing information. They fail from three specific epistemic failures that happen under pressure:

1. **A hedged guess quietly becomes treated as fact.** Someone says "the pool is probably fine," and twenty minutes later the room is acting on that as if it were confirmed.
2. **An explicit decision is forgotten or re-litigated under pressure.** A decision the team already made gets silently reversed with no new evidence, because nobody remembers it was decided.
3. **A destructive action gets verbally approved faster than anyone — human or tool — can check its consequences.** By the time someone realizes a rollback will break two dependent services, the command may already be running.

This is not a generic "incident management is hard" problem. It is specifically an **epistemic drift** problem: the gap between what the team believes and what is actually true widens under time pressure, and nothing in a live voice conversation currently catches that drift before it drives an action.

Existing approaches are insufficient because they arrive too late. A typed warning — a Slack bot, a dashboard alert — surfaces after a command may already be running. Intervening *before* execution requires sub-second, multi-party, live voice; a text-based UI cannot deliver a warning fast enough to matter in the narrow window between "someone proposes an action" and "someone executes it."

---

## 3. Product Vision

AEGIS separates three things that incident response usually blurs together under pressure: **understanding** (what was said), **deciding** (whether it's actually risky), and **authorizing** (whether a human has actually approved it). An LLM only converts messy incident-room speech into typed claims — it never makes a safety call. A deterministic engine evaluates every proposed action against the incident's actual state and system topology. A human explicitly authorizes anything consequential.

The result AEGIS is working toward: fewer irreversible actions taken on unconfirmed guesses, fewer repeated mistakes, less epistemic drift under pressure, and a visible, auditable record of what was decided and why — delivered through real-time voice, because that is the only channel fast enough to intervene before execution rather than after.

---

## 4. Target Users

**Primary users:** SRE and incident-response engineers participating in a live P1/P2 incident call.

**Context:** Multiple engineers (the golden demo scopes this at two participants) join a live voice bridge during an active incident. They are talking to each other, not to AEGIS — diagnosing, forming hypotheses, proposing fixes, and approving actions under real time pressure. AEGIS is a silent participant in that same room, not a tool anyone deliberately opens or queries as their primary workflow.

---

## 5. User Experience

Engineers speak naturally on a live Agora voice call, exactly as they would on any ordinary incident bridge — no special commands, no addressing AEGIS by default.

- **AEGIS listens silently by default.** Through ordinary back-and-forth conversation, it produces no unsolicited output.
- **It detects when something matters.** As the conversation unfolds, AEGIS continuously builds a structured picture of what's been said — facts, hypotheses, decisions, proposed actions — and continuously checks proposed actions against that picture and against system topology.
- **It intervenes, unsolicited, over live audio, when it matters.** When a claim needs correction against reality, or a proposed action conflicts with a prior decision or the system's actual dependency structure, AEGIS speaks up — interrupting, if necessary — and states the exact rule or evidence gap it caught. It does not vaguely flag concern; it names the specific contradiction.
- **It requires explicit human confirmation.** AEGIS never treats an action as authorized on its own judgment. A human must explicitly confirm, deny, or hold before a proposed action's status changes.
- **It answers on request.** An engineer can ask "AEGIS, status?" and receive a spoken summary of open hypotheses, held decisions, and unresolved risk.
- **It closes the loop.** At the end of the incident, it produces a final summary built from the state it actually tracked — not from re-reading the transcript.
- **Multimodal evidence, where implemented:** engineers are not limited to what they say aloud. A participant can also type a short note into a side-channel (treated exactly like a spoken claim) or upload a screenshot — for example, a dashboard panel — which AEGIS interprets and compares against whatever has been claimed conversationally. Voice remains the primary interaction surface; text and screenshots are supplementary evidence channels, not a parallel product experience.

---

## 6. Core Product Capabilities

- **Real-time voice participation** — AEGIS joins a live, multi-party Agora voice call as a participant, not a bot that must be explicitly invoked.
- **Multi-party incident understanding** — it attributes claims to the correct speaker across multiple simultaneous participants.
- **Structured incident state** — it continuously builds a live record of the incident from the conversation, not a raw transcript.
- **Fact / hypothesis / decision / proposed-action distinction** — these are tracked as genuinely different kinds of things, not collapsed into one undifferentiated "claim." A hedge is never treated the same as a stated fact.
- **Staleness awareness** — a hypothesis that has gone unreinforced, or has been superseded, is tracked as stale rather than silently continuing to justify downstream actions.
- **Decision-reversal detection** — if a previously logged decision is contradicted with no new supporting evidence, AEGIS catches it.
- **Topology-aware risk reasoning** — proposed actions are evaluated against the system's actual dependency graph, catching blast-radius consequences a human might miss under pressure (e.g., a rollback that breaks a dependent service on an incompatible schema).
- **Telemetry grounding** — proposed actions and claims are checked against real (in the hackathon build, mocked) system metrics, catching the case where a human is simply wrong about the current state of the system, not just internally inconsistent.
- **Visual evidence grounding** — where the multimodal capability is implemented, a submitted screenshot can ground or contradict a claim the same way telemetry does.
- **Human confirmation** — nothing consequential is ever treated as authorized without an explicit human confirm/deny/hold.
- **Unsolicited voice intervention** — AEGIS decides not just what to say but *whether to interrupt a live human conversation at all*; its intelligence is largely invisible until the moment it correctly does so.
- **Status summaries** — a spoken, on-demand summary of the incident's current open hypotheses, decisions, and risk.
- **Incident timeline** — an ordered record of everything captured during the incident.
- **Final incident summary** — a closing artifact built from the accumulated incident state.

No capability beyond what is listed above (and detailed in the SSOT) is in scope. In particular, AEGIS does not integrate with any real third-party incident-management tool, does not perform open-domain contradiction detection, and does not run as multiple separately-branded "engines" — all risk reasoning happens through one deterministic evaluation.

---

## 7. Multimodal Input

AEGIS's primary interaction surface is live voice. On top of that, the product extends to accept incident evidence through three additional channels, all of which feed the *same* incident-understanding pipeline rather than a separate one:

- **Voice** — the primary channel; ordinary spoken conversation on the incident call.
- **Text** — a participant can type a short message into a lightweight side-channel during the incident; it is treated identically to a spoken claim (fact, hypothesis, decision, or proposed action).
- **Screenshots/images** — a participant can submit a screenshot, such as a monitoring dashboard panel, showing a named metric and value. AEGIS interprets it as a piece of evidence about the real state of the system.
- **Telemetry** — a mocked system-metrics endpoint (four fixed metrics in the hackathon build) that plays the same grounding role as an uploaded screenshot, just fetched automatically rather than submitted by a person.

All four converge on the same incident state and the same risk evaluation. A screenshot showing 91% pool utilization plays exactly the same role as a telemetry reading showing the same value: both are evidence that can confirm or contradict what a person has claimed out loud.

This exists to improve situational awareness and grounding — catching the case where a spoken claim doesn't match what a screenshot or a metric actually shows — not to turn AEGIS into a general-purpose multimodal chatbot. It is explicitly scoped to named-metric, dashboard-style evidence; it is not open-domain image understanding, not a document/OCR pipeline, and not a second AI system running alongside the voice pipeline.

---

## 8. Core User Journey

Incident begins
→ engineers join the live voice call and talk to each other, as usual
→ AEGIS listens (and, where used, also receives typed notes or submitted screenshots)
→ AEGIS builds a shared, structured understanding of what's been said and what's been shown
→ evidence — spoken, typed, visual, or telemetry — enters the incident's shared state
→ a proposed action, or a claim worth checking, triggers a risk evaluation against that state and the system's topology
→ AEGIS decides whether the risk warrants speaking up at all
→ if it does, AEGIS interrupts over live voice and states exactly what's wrong
→ a human explicitly confirms, denies, or holds
→ the incident state updates with that human decision
→ the incident continues, with AEGIS available for an on-demand status check at any point
→ at the end, AEGIS produces a final summary of what happened, what was decided, and what remained open

---

## 9. Functional Requirements

### Must Have
- The system MUST allow two or more distinct human participants to join a live voice channel with AEGIS present.
- The system MUST remain silent through ordinary conversation, producing no unsolicited output absent a genuine trigger.
- The system MUST convert spoken utterances into correctly typed claims (fact, hypothesis, decision, proposed action).
- The system MUST maintain a structured incident state reflecting those claims, distinguishing the four claim types from one another.
- The system MUST evaluate every proposed action through a single, deterministic risk evaluation before any intervention decision is made.
- The system MUST detect at least: stale/unreinforced hypotheses, decision reversals without new evidence, and topology-based blast-radius risk.
- The system MUST intervene over live voice, stating the specific rule or evidence gap violated, when risk warrants it.
- The system MUST require an explicit human confirm/deny/hold before treating any proposed action as authorized.
- The system MUST NOT execute, block, or override any action under any circumstance.
- The system MUST NOT use the LLM itself to make a risk/safety decision.
- The system MUST NOT present an unconfirmed hypothesis using fact-asserting language.

### Should Have
- The system SHOULD ground claims against real (mocked, in this build) telemetry, catching cases where a human is simply wrong about current system state.
- The system SHOULD accept typed text as an additional input channel, treated identically to spoken claims.
- The system SHOULD accept a submitted screenshot as visual evidence and compare it against spoken/typed claims.
- The system SHOULD provide an on-demand spoken status summary.
- The system SHOULD provide a live view of the incident timeline.
- The system SHOULD rate-limit interventions to prevent alarm fatigue.

### Optional / If Time Permits
- Proactive, rate-limited nudges beyond direct risk catches.
- An auto-generated final incident summary as a polished closing artifact.
- A task-ownership treatment in the incident view.
- A visual-evidence UI treatment (e.g., a thumbnail alongside the extracted reading).
- An additional, explicitly optional demo beat showcasing the visual-evidence path — never a replacement for the core demo, and only included with the team's explicit sign-off.

---

## 10. Safety & Trust Requirements

AEGIS's credibility rests on one boundary, held without exception:

> **AI interpretation ≠ deterministic authorization ≠ human authorization ≠ execution**

The LLM only *understands* — it converts speech (and, where used, text and images) into typed claims or evidence. It never decides whether something is risky, and it never decides whether an action is authorized. A deterministic, non-LLM evaluation makes every risk/safety call, using explicit incident state and system topology. Only an explicit human confirmation authorizes a consequential action.

**AEGIS must never:**
- Execute, block, or override any action, under any circumstance — including in a mocked or demo tool call.
- Treat a proposed action as authorized without an explicit, classified human confirmation. Silence, ambiguity, or a timeout is never treated as authorization.
- Let the LLM's output stand in for the risk/safety decision itself.
- Present a hedged or unconfirmed claim using language that asserts it as settled fact.
- Silently alter or misrepresent a decision a human actually made.
- Exceed its intervention rate limit, even in the face of a genuine high-risk event — a second event inside the limit window is re-evaluated once the window reopens, never spoken over the limit and never dropped without a trace.
- Integrate with any real third-party system in a way that could take a real-world action.

Between two kinds of mistakes, the product is deliberately biased toward **catching too much rather than missing something real**: missing a genuine risk (a false negative) is treated as categorically worse than an unnecessary interruption (a false positive) — with the single, deliberate exception of human-confirmation classification, where wrongly treating an ambiguous or absent reply *as* a confirmation is the more dangerous failure.

---

## 11. Non-Goals / Out of Scope

- No new problem statement outside PS4 — the track and problem are fixed by the hackathon's rules.
- No integration with any real third-party system (Jira, Slack, PagerDuty, or otherwise) — all AEGIS "tools" are demo-scoped, read-only, and non-destructive.
- No speaker-diarization machine learning — participant attribution relies on Agora's native per-speaker transcript tagging.
- No multiple, separately-branded "reasoning engines" — all risk evaluation runs through one deterministic function.
- No open-domain contradiction detection beyond the product's fixed set of claim types and checks.
- No Bayesian or probabilistic confidence modeling, for either spoken claims or visual evidence — confidence is handled as a simple categorical signal, never a numeric probability.
- No multi-agent personas or multiple AEGIS "voices."
- No autonomous execution of any action, ever, under any framing.
- No production-grade architecture, scalability engineering, or deployment target — this is a hackathon prototype scoped to a live, in-person demo, not a shippable enterprise product.
- No open-domain image understanding — visual evidence is limited to named-metric, dashboard-style screenshots; no general-purpose computer vision, no OCR-as-a-service, and no separate vision system running alongside the core pipeline.

---

## 12. Hackathon MVP

The MVP is defined as a complete, live, unbroken run of the full loop, demonstrating every one of the following in sequence, without any manual intervention in the pipeline itself:

1. Two distinct participants join the live channel with AEGIS present.
2. AEGIS stays silent through ordinary conversation.
3. Both participants' speech reaches the system without being dropped.
4. Transcripts are correctly attributed to the speaker who actually said them — including through at least one instance of near-simultaneous speech.
5. Extractable claims are correctly typed.
6. Each valid claim lands in the correct part of the incident state.
7. Every proposed action triggers exactly one risk evaluation before any intervention decision.
8. The intervention decision is consistent with the risk evaluation's output.
9. A warranted intervention is actually spoken, and is audible to both participants.
10. A human's subsequent reply is correctly classified as confirm/deny/hold and resolves the pending action.
11. That resolution is correctly reflected in the incident's decision record.
12. A requested or closing summary is produced from the accumulated incident state, not from re-reading the transcript.

This qualifies as a functioning MVP only once all of the above pass together, live, in one unbroken run. Telemetry- and visual-evidence-grounded risk detection (the mechanism behind the demo's first killer moment) is a planned second-stage capability layered on top of this core loop, not a requirement of the MVP loop's first live pass — the MVP's non-telemetry risk detection (staleness, decision-reversal, topology blast-radius) must fire correctly on its own first.

---

## 13. Product Success Criteria

- Two engineers can conduct a real, unscripted-feeling incident conversation with AEGIS present, without needing to address it directly.
- AEGIS maintains a coherent, correctly-typed incident state throughout that conversation.
- AEGIS detects the intended classes of risk — stale hypotheses, reversed decisions, topology blast-radius, and (where evidence is available) contradictions against telemetry or visual evidence.
- When risk is detected, AEGIS intervenes appropriately: audibly, promptly, and stating the specific problem rather than a vague warning.
- Human confirmation is respected in every case — no action is ever treated as authorized without it.
- Where implemented, multimodal evidence (typed text, submitted screenshots) measurably influences AEGIS's reasoning the same way spoken claims and telemetry do.
- The experience holds up reliably enough, across repeated rehearsal, to be shown live to judges without relying on luck.

---

## 14. Demo-Critical Experience

The demo centers on a single scripted incident conversation between two engineers, joined as distinct participants with AEGIS listening. The experience that must work reliably during judging is the same experience described in Section 5 — silent by default, correctly attributing speech to each speaker, and intervening at the right two moments.

**The two finalized killer moments:**

1. **Reality-grounding catch.** One engineer states a hypothesis about system state ("pool utilization looks fine, around 40%"). AEGIS checks that claim against the actual (mocked) telemetry, finds it contradicted (91%), and interrupts — grounding a claim against reality, not merely against what else has been said in the conversation.
2. **Compound catch.** Building directly on the first catch, an engineer proposes a rollback. AEGIS catches two independent problems at once: the earlier hypothesis about root cause is still unconfirmed, and the proposed rollback would break two dependent services incompatible with the target schema — a genuine topology-aware consequence catch, not a scripted trick repeated for effect.

These two moments together — not one capability shown twice — are what the demo is built to prove. An optional additional beat demonstrating the same reality-grounding pattern via a submitted screenshot instead of mocked telemetry exists as a candidate addition, but is explicitly not part of the locked demo unless the team decides, with sign-off, to include it.

---

## 15. Hackathon Scope

AEGIS is explicitly a **hackathon-scoped prototype**, built for a live, judged demo within a hard multi-day deadline — not a production incident-management product.

- **Core competition functionality:** the full live voice loop — multi-party speech understanding, structured incident state, deterministic risk evaluation (staleness, decision-reversal, topology), voice intervention, and human confirmation — running end to end at least once, live.
- **Phase 2 improvements:** telemetry-based reality grounding; typed-text and screenshot evidence ingestion; measured (not estimated) accuracy, reliability, and latency, with adversarial testing of the risk-detection chain.
- **Phase 3 polish:** a usable incident-state view, rehearsal-hardened reliability, a recorded fallback for the judged session, and — only if the multimodal work has proven reliable — an optional bonus demo beat.

No deployment requirements are defined for this product; it is understood to be fully local and demo-scoped for the duration of the hackathon.

---

## 16. Product Constraints

- **Hard hackathon deadline** — a fixed, non-negotiable submission date drives every scope decision; features are deliberately staged as Must/Should/If-Time rather than all attempted at once.
- **Fixed problem statement (PS4)** — the product cannot pivot to a different track or problem framing.
- **Agora as the primary real-time conversational layer** — live, multi-party voice with per-speaker attribution and barge-in intervention is the mechanism the entire product depends on; there is no non-Agora fallback for the core experience.
- **Human-in-the-loop safety by design** — every consequential action requires explicit human authorization; this is a constraint on the product, not a limitation to be engineered around.
- **Demo-scoped telemetry** — reality-grounding uses a small, fixed set of mocked metrics, not a real monitoring integration.
- **Controlled, bounded multimodal scope** — text and image input exist strictly to ground incident reasoning, not to expand AEGIS into a general assistant; visual evidence is limited to named-metric, dashboard-style readings.
- **No real third-party integrations** — nothing in the product can take a real-world action outside the demo environment.

---

## 17. Final Product Definition

AEGIS is a voice-native AI participant that joins a live incident call and listens silently by default. As engineers speak — and, where used, as they submit typed notes or screenshots — AEGIS builds a structured, trustworthy picture of what has actually been established: what's a fact, what's a hedge, what's been decided, and what's being proposed. Every proposed action is checked, by deterministic logic rather than the AI's own judgment, against that picture, against the system's real dependency structure, and against whatever telemetry or visual evidence is available. When that check turns up a real problem — a stale guess, a silently reversed decision, a destructive blast radius, or a claim that doesn't match reality — AEGIS interrupts the live conversation, states exactly what's wrong, and waits for an explicit human decision before anything is treated as authorized. It never acts on its own. It is not a notetaker, and it is not a chatbot waiting to be asked something — it is an unsolicited-intervention safety layer for the moments in a live incident when a hedged guess is about to become a costly mistake.
