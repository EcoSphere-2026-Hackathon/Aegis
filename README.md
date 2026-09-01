# AEGIS

A voice-native AI participant that joins a live incident call, builds a
trustworthy picture of what has actually been established, and interrupts —
over live audio — when a proposed action rests on something that isn't true.

It is not a notetaker and not an assistant waiting to be asked. Nobody talks
to it. It listens, and the only evidence it is working is that it stays quiet
until the moment it shouldn't.

---

## Run it

```bash
pip install -r requirements.txt

python run.py --demo     # replay the rehearsed demo in the terminal
python run.py            # serve the console at http://127.0.0.1:8080
python run.py --check    # print the resolved config and any warnings
```

No configuration is needed for either. With no LLM key the extractor falls
back to a rule-based offline provider that carries the demo unaided; with no
Agora credentials everything works except live voice.

```bash
python -m unittest discover -s backend/tests -t .   # 370 tests
python scripts/run_golden_demo.py                   # demo as a regression gate
python scripts/evaluate.py                          # 20 adversarial scenarios
python scripts/eval_extraction.py                   # extraction vs a labelled set
python scripts/check_agora.py                       # voice preflight + manual checklist
python scripts/benchmark.py                         # latency and read-scaling
```

`GET /api/metrics` reports the same numbers from a running server, so every
performance claim below can be checked rather than taken on trust.

---

## What it does

```
speech ─┐
text ───┼─→ extraction ─→ incident state ─┐
        │   (LLM: understands only)       │
        │                                 ↓
screenshot ─→ evidence ──────────→ risk_engine.evaluate()
telemetry  ─→ evidence ──────────→ (Python: decides, deterministically)
                                          │
                                          ↓
                                  intervention governor
                                  (≤1 spoken / 45s)
                                          │
                                          ↓
                            Agora speak(priority: INTERRUPT)
                                          │
                                          ↓
                        human confirms / declines / holds
                                          │
                                          ↓
                                  state updated
```

The line that matters:

> **AI interpretation ≠ deterministic authorization ≠ human authorization ≠ execution**

The LLM converts messy speech into typed claims. It never outputs a risk
level and has no field in its response schema in which it could. Every
risk decision is made by Python comparing typed fields. Every consequential
action waits for an explicit human utterance. AEGIS has no execution surface
at all — its entire outbound vocabulary is "join a call", "say this",
"leave".

### The two catches

**Grounding a claim against reality.** Someone says "pool utilization looks
fine, like 40%". Telemetry says 91%. AEGIS says so, before anyone acts on the
40%. This is what makes it a grounding system rather than a consistency
checker: it catches a human being *wrong about the world*, not merely
inconsistent with themselves.

**A compound catch on a destructive action.** Someone proposes rolling Core
back. Two independent problems, in one interruption: the root cause was
contradicted and never re-established, and the rollback breaks two dependents
on an incompatible schema. Nobody said "v2.3" out loud — that came from the
topology, because a rollback's landing version is a property of the system,
not of the sentence.

### What it does that a rules engine does not

Four decisions in this codebase are the ones worth reading. Each replaced
something simpler that was *correct* and quietly wrong for the job.

**Retracting a belief re-opens what rested on it.** Every proposed action
records the hypothesis that justified it, so the store holds a justification
graph. When reality invalidates a theory, the pipeline walks that edge
backwards (`pending_actions_justified_by`, an indexed reverse lookup) and
re-evaluates the unresolved actions standing on it. Without this there is a
silent hole with a very bad shape: an action evaluated as low risk *because a
theory supported it*, still pending, still carrying a verdict computed
against a belief nobody holds any more. It only ever escalates, it cannot
cascade (re-evaluating an action produces a verdict, never a hypothesis
change), and it is capped per turn.

**The scarce channel is scheduled, not queued.** One intervention per 45
seconds makes speaking a scarce resource, so *which* warning is delivered
when the window reopens is an admission-control decision. Entries carry a
utility — tier, plus independent findings, decayed by an exponential
half-life — and the window goes to the maximum. Arrival order is not a
proxy for importance: FIFO spends the window on whichever risk happened to be
mentioned first and makes a schema-breaking rollback wait behind a service
restart. The linear scan over the queue is deliberate rather than lazy:
utility is time-varying, which breaks a heap's stable-key assumption.

**Fitting the 512-byte budget is a packing problem.** Agora caps `speak` text
at 512 bytes. Greedy-by-severity takes whichever equal-tier finding comes
first and can then afford nothing else; one verbose MEDIUM can crowd out two
concise ones that together say more. It is a 0/1 knapsack — items with a
value and a byte cost, one capacity — so it is solved as one, exactly, by
dynamic programming over the *rendered* cost of each sentence. The most
severe finding is mandatory (it sets the intervention's tier); the rest
compete.

**Ambiguity is refused, not guessed.** Deciding which pending action a "yes"
answers is the one place a human utterance changes the authorisation state of
something consequential. `pipeline/resolution.py` is a pure, total policy: a
named target decides it; a bare reply requires exactly one open action and
has to be timely, measured from the last moment that action was live in the
room. When it cannot be sure it returns AMBIGUOUS, AEGIS asks which one was
meant, and nothing is authorised — which is the correct resting state.

**Blast radius is transitive.** Naming only the direct dependents of a
service understates the damage. `propagate_failure` runs a multi-source
reverse BFS over the transposed dependency graph — O(V+E) regardless of how
many services are named — and separates direct breakage from the cascade and
from the user-facing entry points it reaches. In the demo topology that is
the difference between reporting 2 affected services and 6.

### Where the demo breaks, and what stops it

`run.py --reset` and `POST /api/reset` exist because the highest-probability
failure in a live demo is being asked to run it a second time. The store is a
file by default, so the second run would inherit the first one's pending
actions, spent turn ids and — worst — the governor's closed rate-limit window
and full already-said set, producing a run in which AEGIS says nothing at all
and looks broken for a reason nobody can see from the outside.

`scripts/check_agora.py` is the other half: it validates the configuration
without a network call, optionally joins/speaks/leaves to prove the
credentials, and then prints the checklist of what preflight *cannot* answer —
each item naming the assumption, how to test it with a real channel, and what
it breaks if it is wrong.

---

## Layout

```
backend/
  common/        models, enums, typed errors, config + secret redaction,
                 injectable clock, structured logging
  risk_engine/   topology graph, the four checks, evaluate(), staleness
  state_store/   SQLite schema + a thread-safe transactional repository
  extraction/    provider protocol, versioned prompt, validation,
                 providers/{openai_compatible,deterministic}
  governor/      rate-limited state machine + speech composition
  telemetry/     four mocked metrics
  pipeline/      the closed loop, event bus, sinks, worker, assembly
  agora/         Conversational AI Engine client
  api/           Starlette routes, auth guard, rate limiter
  tests/         370 tests
frontend/        zero-build operator console (served by the backend)
scripts/         golden-demo replay, evaluation harness, benchmark
data/            hand-authored scenarios with expected outcomes
```

### Where the invariants live

| Invariant | Enforced by |
|---|---|
| Nothing is authorised without an explicit human utterance | `IncidentStateStore.resolve_proposed_action` — a conditional `UPDATE … WHERE status='pending'` with a rowcount check, so two concurrent confirmations cannot both win and a resolved action can never be silently re-resolved |
| The LLM never decides risk | The response schema has no risk field; `risk_engine` imports no provider |
| At most one intervention per window | One code path in `Governor.decide`, measured on a **monotonic** clock so a wall-clock jump cannot walk through it |
| A non-LOW verdict always explains itself | `RiskVerdict` raises on construction — not an `assert`, which `python -O` removes |
| Timestamps are comparable and orderable | Naive datetimes are rejected at the model boundary; the timeline is ordered by event time, not write order |
| Secrets never reach a log | `Secret` redacts in `repr`; the log redactor also matches by key name, recursively |

---

## Honest status

**Verified in tests.** The whole reasoning loop: extraction, state, the four
risk checks, the governor, human resolution, the API surface, concurrency,
and both demo moments end to end.

**Not verified — needs the live Agora spike.** Everything that can only be
answered by running it:

- whether `speak(priority: INTERRUPT)` actually cuts through a human who is
  mid-sentence;
- whether `turn_detection.start_of_speech.mode: "manual"` really keeps the
  agent silent through ordinary conversation, or whether the fallback of
  bypassing the agent's LLM slot is needed;
- multi-speaker UID attribution under overlapping speech;
- real end-to-end latency (the ~1–2s figure is an estimate, and is labelled
  as one everywhere it appears);
- whether the Agora Customer ID/Secret pair exists in the Console at all.
  This is the one credential that can block the live demo, and it has never
  been confirmed.

The Agora client implements the documented contracts and keeps the unverified
parts configurable, so a spike result can be absorbed by changing
configuration rather than rewriting the integration.

**Deliberately mocked, and disclosed rather than hidden.** Four fixed
telemetry metrics, a ten-node topology, a twenty-scenario evaluation set
and thirty labelled utterances.
Each is a scope decision made against a three-day deadline, and none of them
is presented as more than it is.

**Not built.** No third-party integrations. No production or scalability
architecture. No user accounts — the authorization model *is* the human
confirmation boundary. The one security control that exists is a shared
bearer token on the ingestion endpoints, because an unauthenticated endpoint
that accepts a `confirmation` claim is a direct path to an action being
treated as authorised that nobody approved.

---

## Performance

`python scripts/benchmark.py`, 200 turns through the real pipeline with the
deterministic provider and an in-memory store. A hosted LLM adds its own
latency, which is why extraction is reported separately.

| | p50 | p95 |
|---|---|---|
| whole turn | 0.43 ms | 1.74 ms |
| extraction | 0.05 ms | 0.12 ms |
| risk evaluation | 0.16 ms | 0.21 ms |
| working-set read | 0.25 ms | 0.39 ms |

Read cost was the thing that actually scaled badly. Evaluating one action
against a full incident snapshot is O(incident) per turn — correct, and
steadily slower for as long as the incident lasts:

| turns so far | full snapshot | working set | rows read |
|---|---|---|---|
| 20 | 0.220 ms | 0.028 ms | 13 → 3 |
| 100 | 0.859 ms | 0.029 ms | 59 → 3 |
| 400 | 3.245 ms | 0.026 ms | 229 → 3 |

The working set is flat because it asks for what the checks actually read —
the justifying hypothesis and the decisions about this target — rather than
the whole incident. `latest_evidence_per_metric` does the same for readings,
folding to one row per metric in SQL (`ROW_NUMBER() OVER PARTITION BY`)
instead of loading every reading and folding in Python.

96% of extraction requests avoided a provider round trip in that run: the
backchannel fast path answered 27% locally, and the response cache the rest.
The cache figure is an upper bound — the benchmark repeats a fixed rotation
of utterances by construction — so the fast-path number is the one that
transfers to a real conversation, and even that is now conditional: the fast
path is switched off entirely while an action is awaiting a decision, because
"yeah" is filler in open conversation and an answer when AEGIS has just asked
a question, and an optimisation that can swallow a human's answer is not one.

---

## Observability

One JSON object per line, one event per pipeline stage, correlated by turn:
transcript received, claims extracted or rejected, state mutated, risk
evaluated, governor decided, speak called, human resolved. A rehearsal can be
reconstructed from the log alone — which stage was slow, why a verdict came
out as it did, why AEGIS spoke or stayed quiet.

Set `AEGIS_LOG_FILE` to keep it. The same format is the evaluation harness's
input, which is why it is defined once and reused rather than rewritten
later.

---

## Notes for whoever picks this up

- **Python 3.10** is the floor (`match`-free, no `StrEnum`, `timezone.utc`
  rather than `datetime.UTC`).
- **The clock is injected.** Anything time-dependent takes a `Clock`; tests
  use `ManualClock` and never `sleep`. Rate limiting uses `monotonic()`,
  event timestamps use `now()`, and mixing them up is a bug.
- **`risk_engine` is pure.** It returns determinations; the state store
  applies them. If you find yourself wanting to write to the store from a
  check, the design has drifted.
- **The extractor's vocabulary is data, not code.** Component names come from
  the topology, metric names and the phrases people use for them come from
  the telemetry catalogue. Adding a service or a metric should not require
  editing the extractor.
- **The harnesses earn their keep.** `scripts/evaluate.py` caught two real
  bugs during the build: a decision-to-hold recorded with the opposite
  stance, and the engine citing its own telemetry poll as new evidence that
  cleared a reversal. `scripts/benchmark.py` caught a third: the extraction
  cache keyed on the *ordered* tuple of pending targets, so it missed on
  every reordering and its hit rate was ~0 in exactly the long incidents it
  exists for. All three have regression tests. Expectations in the evaluation
  set are authored from what the system *should* do — never from what it
  currently does, which would make the whole exercise circular.
