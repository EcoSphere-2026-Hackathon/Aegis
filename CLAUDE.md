# AEGIS — working notes for Claude Code

Read this before changing anything. It carries the context that is not
recoverable from the code alone: which decisions are load-bearing, which
things look wrong but are deliberate, what has already been tried and
rejected, and what is genuinely still unproven.

---

## What this is

A voice-native AI participant that joins a live incident call, builds a
trustworthy picture of what has actually been established, and interrupts —
over live audio — when a proposed action rests on something that isn't true.

Not a notetaker. Nobody talks to it. It listens, and the only evidence it is
working is that it stays quiet until the moment it shouldn't.

Hackathon project (EcoSphere / Agoda). Repo:
`github.com/EcoSphere-2026-Hackathon/Aegis`, branch `main`.
Local path on the owner's machine: `C:\Users\ayush\Documents\Ecosphere`.

**The line the whole design rests on:**

> AI interpretation ≠ deterministic authorization ≠ human authorization ≠ execution

---

## Hard rules — do not break these

These are spec red lines, not preferences. Several have tests whose only job
is to fail if someone "simplifies" them.

1. **AEGIS never executes, blocks or overrides anything.** Its entire
   outbound vocabulary is *join a call*, *say this*, *leave*. There is no
   code path to act on the systems being discussed. Do not add one.
2. **Nothing is authorised without an explicit, classified human utterance.**
   Silence, ambiguity and timeouts leave an action `pending` forever. There
   is deliberately no timeout path into resolution.
3. **The LLM never decides risk.** Its response schema has no field in which
   it could. `risk_engine` imports no provider and holds no connection.
4. **≤1 spoken intervention per 45s**, measured on a *monotonic* clock. No
   exception for a genuine double-HIGH event — that is explicitly in the spec
   and has a scenario.
5. **Speech is capped at 512 bytes** (Agora `/speak`), enforced client-side
   before the request.
6. **No hypothesis is voiced in fact-asserting language.** Sentences are
   built from typed findings, never by interpolating claim text into an
   assertive template.
7. **Secrets never reach the frontend, a log, or version control.** `Secret`
   redacts in `repr`; the log redactor matches by key name recursively; error
   contexts carry status codes, never response bodies (a body can echo the
   request, and the request carries the Authorization header).

---

## Architecture

One process, one SQLite file, one in-process queue. ~10,000 lines of backend.
**Deliberately a modular monolith.** No Kafka, no Redis, no Kubernetes, no
microservices, no second datastore. There is one incident, one room, and a
requirement that a verdict is produced between two sentences of human speech;
a network hop makes that worse, not more impressive. Do not "scale" this.

```
backend/
  common/        models (pydantic v2, frozen), enums, typed errors, config +
                 Secret redaction, injectable Clock, structured logging, metrics
  extraction/    provider protocol, versioned prompt, validation,
                 providers/{openai_compatible,deterministic}
  risk_engine/   topology graph, the four checks, evaluate(), staleness
  state_store/   SQLite schema + thread-safe transactional repository
  governor/      rate-limited state machine + speech composition
  pipeline/      orchestrator (the loop), resolution (confirmation policy),
                 delivery (speaking), events, sinks, worker, factory
  telemetry/     four mocked metrics
  agora/         Conversational AI Engine client + AgoraSpeechSink
  api/           Starlette routes, auth guard, rate limiter
  tests/         370 tests
frontend/        zero-build console at / and landing walkthrough at /hero
  design/        approved design sources (not runnable, provenance only)
scripts/         harnesses and operational tools
data/            fixtures: golden demo, adversarial scenarios, extraction labels
```

### The turn lifecycle

```
transcript → claim_turn (idempotency) → extraction (OUTSIDE the lock)
  → [lock] state → working set → risk → governor [/lock]
  → delivery (OUTSIDE the lock) → SSE → frontend
```

Two things are deliberately outside the state lock because both are slow and
neither transitions state: **extraction** (model call, multi-second timeout)
and **delivery** (`sink.speak`, an 8-second Agora HTTP timeout). Delivery
collects into a per-thread outbox and flushes after every lock is released.
There is a test that grabs the lock while a deliberately slow sink is
mid-call; if it fails, someone put speaking back under the lock.

---

## The five algorithms worth knowing

Each replaced something simpler that was *correct* and quietly wrong for
this job. If you find yourself thinking one is over-engineered, the note
after it is why it isn't.

1. **Justification graph / belief retraction.** Every proposed action records
   the hypothesis that justified it. When reality invalidates a theory, the
   pipeline walks the reverse edge (`pending_actions_justified_by`, partial
   index) and re-evaluates the unresolved actions resting on it. It only
   escalates, cannot cascade (re-evaluating an action yields a verdict, never
   a hypothesis change), and is capped at 8/turn. *Without it: an action
   evaluated LOW because a theory supported it, still pending, still carrying
   a verdict computed against a belief nobody holds.*
2. **Utility scheduling of the intervention queue.** Tier + independent
   findings, decayed by a 90s half-life; the window goes to the maximum. The
   **linear scan is deliberate, not lazy** — utility is time-varying, which
   breaks a heap's stable-key assumption. *FIFO makes a schema-breaking
   rollback wait behind a service restart mentioned first.*
3. **Exact 0/1 knapsack for the 512-byte speech budget**, over the *rendered*
   cost of each sentence. The lead (most severe) finding is mandatory; the
   rest compete. *Greedy takes the first equal-tier finding and can then
   afford nothing.*
4. **Multi-source reverse BFS on the transposed dependency graph**
   (`propagate_failure`), O(V+E). Separates direct breakage from cascade from
   user-facing entry points. *Direct dependents alone report 2 services where
   6 break.*
5. **Confirmation attribution as a pure, total policy** (`pipeline/
   resolution.py`). Named target decides it; a bare reply requires exactly
   one open action *and* must be timely, measured from the last moment that
   action was live in the room. Refuses rather than guesses. A non-RESOLVED
   `ResolutionDecision` **structurally cannot carry an action** — the
   dataclass raises. *The heuristic it replaced returned `pending[-1]` and
   logged a warning.*

---

## Commands

```bash
pip install -r requirements.txt

python run.py                    # serve console at :8080 (/ and /hero)
python run.py --demo             # replay the golden demo in the terminal
python run.py --check            # resolved config + warnings
python run.py --reset            # clear the stored incident, then serve

python -m unittest discover -s backend/tests -t .        # 370, ~12s
python scripts/run_golden_demo.py                        # demo as a gate
python scripts/evaluate.py                               # 20 adversarial
python scripts/eval_extraction.py                        # 30 labelled utterances
python scripts/benchmark.py                              # latency + read scaling
python scripts/check_agora.py [--live] [--checklist]     # voice preflight
python scripts/check_frontend.py --scenario              # needs a running server
ruff check .            # config in pyproject.toml
mypy backend scripts run.py
```

`check_frontend.py` drives headless Chromium and fails on any console error,
page exception or failed request. `--scenario` drives a whole incident
through the console's own text box. Compress the rate limit for it:
`GOVERNOR_RATE_LIMIT_SECONDS=2`.

**Current state: 370 tests / 20 scenarios / 30 labelled utterances, all
green. ruff and mypy clean across 51 files.**

---

## Environment gotchas

- **Target is Python 3.10** even though the dev container runs 3.11. No
  `StrEnum`, no `datetime.UTC` (use `timezone.utc`), no `typing.Self`.
  Verify with `python3 -m compileall` on 3.10 before shipping.
- **The database is a file by default** (`data/incident.db`). This is the
  single highest-probability demo failure: a second run inherits the first
  one's pending actions, spent turn ids, and — worst — the governor's closed
  rate-limit window and full already-said set, producing an AEGIS that says
  **nothing at all**. Use `run.py --reset` or `POST /api/reset` between takes.
- `scripts/` is a package (`__init__.py`) because `run.py --demo` imports it;
  without that, mypy sees the same file under two module names and refuses.
- The clock is injected everywhere. Tests use `ManualClock` and never sleep.
  **Rate limiting uses `monotonic()`, event timestamps use `now()`** —
  mixing them up is a bug.

---

## Frontend

Vanilla, zero build step, no package.json, no bundler, no CDN. That is
deliberate: the demo must run on a laptop with no toolchain and no network.
Do not introduce a build step.

- `/` — operator console (`index.html`, `app.js`). Reads `/api/state` for
  authoritative state and `/api/events` (SSE) as a *hint that something
  changed*, never as the source of truth. Rebuilding derived state from a
  partial event feed is how a UI quietly disagrees with the system.
- `/hero` — landing walkthrough (`hero.html`, `hero.js`, `hero.css`,
  `topology3d.js`). Replays the rehearsed golden demo into the console's own
  markup (same classes, same `styles.css`, `f-` prefixed ids) so the filmed
  console cannot drift from the real one. Hydrates from `/api/topology`,
  `/api/telemetry`, `/api/health`, `/api/metrics`; falls back to a shipped
  fixture and **says which it is showing**.
- `topology3d.js` is a hand-rolled 2D canvas projection — no WebGL, no
  three.js, on purpose (offline guarantee). It has label-collision
  suppression, dirty-flag repainting, IntersectionObserver idling, and a
  `destroy()` that actually disconnects everything.
- Assets are referenced as `/static/...` absolute paths; the backend serves
  `frontend/` at `/static` and the two pages at `/` and `/hero`.
- **The frontend contains no Agora code at all.** Voice is entirely
  server-side. There is no client SDK and no token endpoint — keep it that way.

---

## API surface

```
GET  /api/health      status, extraction_provider, agora_authenticated, window_open
GET  /api/state       full incident; ETag + 304 (store version as validator)
GET  /api/topology    {nodes, edges:[{from,to,type,schema_version?}]}
GET  /api/telemetry   {metrics:[{name, unit, target_ref, description, current_value}]}
GET  /api/metrics     {stages:{<stage>:{count,p50_ms,p95_ms,max_ms,mean_ms}}, counters, extraction, scheduling, ingest}
GET  /api/events      SSE
POST /api/transcript  202 new / 200 {"duplicate":true}   [token]
POST /api/text        typed side-channel                  [token]
POST /api/evidence    screenshot/telemetry reading        [token]
POST /api/telemetry/set  move a mocked metric             [token]
POST /api/reset       clear the incident                  [token]
```

Note `/api/metrics` shape: `stages.<stage>.p50_ms`. An earlier frontend read
`metrics.latency_ms.turn_p50`, which does not exist — the perf strip silently
showed hardcoded numbers under copy claiming they were live. Fixed; don't
regress it.

---

## History: what previous passes found and fixed

Two full audits have run. Do not redo this work; do not assume it is right
either — it is evidence, not truth.

**Audit 1 (backend hardening):** FIFO queue → utility scheduling; no belief
retraction → justification graph; greedy speech → exact knapsack; direct-only
blast radius → transitive reverse BFS; full snapshots → working-set reads;
no instrumentation → `/api/metrics` + benchmark. Plus four defects: 512-byte
budget could overrun by 2 bytes (count word "two"→"three"); extraction cache
keyed on an *ordered* tuple so it never hit (30%→94% avoidance when fixed);
unbounded already-said set that gagged permanently; unguarded cache after
extraction moved outside the lock.

**Audit 2 (final hardening):** six more defects —

1. Confirmation attribution guessed (`pending[-1]`) → the resolution policy.
2. **A hold spoken as a *reply* left no Decision in the ledger**, so the
   reversal check had nothing to read → propose / "no, hold off" / re-propose
   was *completely silent* on any low-blast-radius target. Resolutions now
   write a Decision with the matching stance.
3. **A regression I introduced:** the echo guard matched any terminal status,
   silently disabling decision-reversal detection for a 2-minute window.
   Caught because the benchmark's risk-evaluation count dropped 40→14.
   Now scoped to CONFIRMED only. *This is why the instrumentation exists.*
4. A second person agreeing ("yes, roll back X" after it was confirmed) was
   re-read as a fresh proposal and re-opened the settled action.
5. Delivery ran under the state lock (8s Agora timeout froze everything).
6. Idempotency lived in the HTTP handler only — the pipeline itself
   double-counted, and the RTM path bypassed it entirely.

Also: the backchannel fast path could swallow a human's "yeah" answer (now
disabled while anything is pending); confidently-stated causes were filed as
settled *facts* when the thing blamed wasn't in the topology.

**Audit 3 (frontend integration):** integrated the approved landing
walkthrough into `frontend/`, fixed the `/api/metrics` shape mismatch, added
`/hero` route, fixed five canvas issues (off-screen redraw, undisconnected
observers, missing ResizeObserver, blanking resize, overlapping labels).

---

## Remaining limitations — be honest about these

- ❌ **Live voice has never run end to end.** Whether `priority: INTERRUPT`
  cuts through a human mid-sentence, whether manual turn detection keeps the
  agent quiet, whether speaker UIDs survive overlapping speech, and whether
  the Agora Customer ID/Secret pair even exists for the account — all
  unverified. `scripts/check_agora.py --checklist` prints the nine items with
  how to test each and what it breaks. 31 tests cover *our* side of the
  contract against a mock transport.
- ❌ **The RTM transcript relay does not exist.** `/api/transcript` has no
  frontend caller. Something must POST transcripts for live voice to work.
- ❌ **The real LLM extraction path has never been run.** The harness scores
  it the moment credentials exist. The 30/30 deterministic score is **not
  independent** — two cases failed, the underlying defects were fixed, they
  then passed. Read it as "handles these thirty cases", not a generalisation.
- ⚠️ **Justification association is still a heuristic** (same component →
  metric describing it → most recent theory in window). A wrong association
  means the wrong belief retraction re-opens an action. Bounded: it produces
  a wrong *warning*, never a wrong *authorisation*.
- ⚠️ `orchestrator.py` is ~1,040 lines. Delivery was extracted (a real seam);
  the rest is genuinely one loop and the next split would be arbitrary.
- ⚠️ Queues are in-memory; a restart mid-incident loses what was queued. The
  rate limiter is per-process and keyed on client identity. Correct calls for
  one process serving one room for one weekend.
- ⚠️ Telemetry is four fixed metrics and the topology a ten-node fixture. The
  reasoning is real; the graph it reasons over is a file someone typed.
- ⚠️ The console takes its ingest token from `?token=` in the URL.
  Pre-existing hackathon scope decision.

---

## Working style the owner expects

- Priority order: **correctness > security > reliability > maintainability >
  scalability > performance > clean architecture > developer experience.**
- Refactor weak code rather than preserving it because it exists; leave good
  code alone rather than rewriting it for style.
- **No architecture theater.** Nothing added because it sounds impressive.
  Optimise for maximum technical impression per unit of complexity.
- Say directly when the owner's proposed approach is technically inferior.
- Never claim something works because it compiles. Classify honestly:
  ✅ verified / ⚠️ partial / ❌ failed / ⏳ needs credentials.
- Don't declare anything "perfect" because a task finished.

**The backend is considered frozen** unless a concrete integration bug turns
up. Further backend changes now carry a higher probability of introducing a
bug than of creating hackathon value — audit 2's self-inflicted regression is
the evidence.
