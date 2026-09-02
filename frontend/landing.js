/* AEGIS landing page — the narrative sections.
 *
 * Vanilla, same reasoning as app.js and hero.js: this page is served by the
 * Python backend off /static and has to run on a demo laptop with no
 * toolchain and no network.
 *
 * Division of labour on this page:
 *   hero.js     owns the filmed console, its transport and the topology
 *               explorer — the deterministic replay of the golden demo.
 *   landing.js  (this file) owns everything else: the reasoning spine in the
 *               hero, the four gates, the eight stages, and the three
 *               explanatory demos (belief retraction, ambiguity, governor).
 *
 * Two rules this file holds to:
 *
 *   1. No frame loop. Every motion here is a CSS transition or keyframe
 *      triggered by a data attribute; JavaScript only flips attributes, on
 *      coarse timers, and stops them when the section is off-screen. A page
 *      with this many animated sections cannot afford a rAF per section.
 *
 *   2. Nothing invents a capability. The demos below are explanatory
 *      visualizations of algorithms that run server-side, and each says so on
 *      screen. The only live data on this page comes from read-only API
 *      endpoints, and where a read fails the page says what it is showing
 *      instead of quietly presenting a fixture as a measurement.
 */

(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const el = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;   // textContent, never innerHTML
    return node;
  };
  const byteLength = (text) => new TextEncoder().encode(text).length;
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* ── entering the console ────────────────────────────────────────────
   * The console takes its ingest token from ?token= in the URL (a
   * pre-existing hackathon scope decision). A judge who opened this page
   * with a token has to arrive at /command still carrying it, or the typed
   * side-channel starts failing with a 401 the moment they try it.
   */
  const search = window.location.search;
  document.querySelectorAll("a[data-enter]").forEach((link) => {
    link.setAttribute("href", "/command" + search);
  });

  /* ── section observation ─────────────────────────────────────────────
   * One helper for "do something when this element is on screen", so every
   * section shares a single, consistent policy: reveal once, and start/stop
   * anything that ticks. Sections that never animate never get an observer.
   */
  function onScreen(node, { enter, leave, threshold = 0.2, once = false } = {}) {
    if (!node) return;
    if (!window.IntersectionObserver) { if (enter) enter(); return; }
    const observer = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) {
          if (enter) enter();
          if (once) observer.disconnect();
        } else if (leave) {
          leave();
        }
      });
    }, { threshold });
    observer.observe(node);
  }

  /* ══ NAVIGATION ═══════════════════════════════════════════════════════ */

  (function nav() {
    const links = Array.from(document.querySelectorAll(".nav-links a"));
    if (!links.length || !window.IntersectionObserver) return;

    const targets = new Map();
    links.forEach((link) => {
      const node = document.getElementById(link.getAttribute("href").slice(1));
      if (node) targets.set(node, link);
    });

    // Track which sections are on screen and mark the topmost one, rather
    // than marking whatever fired last — with sections this tall, two are
    // visible at once for most of the page.
    const visible = new Set();
    const observer = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) visible.add(entry.target);
        else visible.delete(entry.target);
      });
      const current = Array.from(visible).sort((a, b) => a.offsetTop - b.offsetTop)[0];
      targets.forEach((link, node) => {
        if (node === current) link.setAttribute("aria-current", "true");
        else link.removeAttribute("aria-current");
      });
    }, { rootMargin: "-70px 0px -55% 0px", threshold: 0 });

    targets.forEach((_link, node) => observer.observe(node));
  })();

  /* ══ HERO: THE REASONING SPINE ════════════════════════════════════════
   * Seven stations, each carrying the artifact that actually exists at that
   * stage. The picture is making one argument: the model owns exactly one of
   * the seven, and it is not the one that decides anything.
   */

  (function spine() {
    const list = $("l-spine");
    const caption = $("l-spine-caption");
    if (!list) return;

    const stops = Array.from(list.children);
    const CAPTIONS = [
      "Two engineers on a live bridge. AEGIS is on the call, and nobody talks to it.",
      "The model's only job: turn what was said into a typed claim, and stop.",
      "The claim becomes state — a theory with a status, a metric and an owner.",
      "Telemetry is the ground. The claimed number and the measured one stay separate objects.",
      "Python compares the two and sets the tier. The model is not consulted here.",
      "One channel, 512 bytes. This is the first thing it has said all call.",
      "And it stops. There is no timeout path past this station — only a person.",
    ];

    const STEP_MS = 950;
    const HOLD_MS = 3400;
    let step = -1;
    let timer = 0;
    let running = false;

    const clearTimer = () => { if (timer) { clearTimeout(timer); timer = 0; } };

    function reset() {
      stops.forEach((stop) => {
        stop.removeAttribute("data-active");
        stop.removeAttribute("data-flow");
      });
      step = -1;
    }

    function advance() {
      step += 1;
      if (step >= stops.length) { reset(); step = 0; }

      stops[step].setAttribute("data-active", "true");
      if (step < stops.length - 1) stops[step].setAttribute("data-flow", "true");
      if (caption) caption.textContent = CAPTIONS[step] || "";

      const last = step === stops.length - 1;
      timer = setTimeout(advance, last ? HOLD_MS : STEP_MS);
    }

    function start() {
      if (running || reduced) return;
      running = true;
      clearTimer();
      timer = setTimeout(advance, 260);
    }

    function stop() {
      running = false;
      clearTimer();
    }

    if (reduced) {
      // No autoplay: show the whole pipeline at rest, which is the state the
      // animation exists to arrive at anyway.
      stops.forEach((s) => s.setAttribute("data-active", "true"));
      if (caption) caption.textContent = CAPTIONS[CAPTIONS.length - 1];
      return;
    }

    onScreen($("l-spine-frame"), { enter: start, leave: stop, threshold: 0.25 });
    // A timer in a background tab is throttled rather than stopped, and a
    // walkthrough that advanced six stations while nobody was looking is
    // resumed mid-sentence.
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) stop();
      else if (isOnScreen($("l-spine-frame"))) start();
    });

    function isOnScreen(node) {
      if (!node) return false;
      const rect = node.getBoundingClientRect();
      return rect.bottom > 0 && rect.top < window.innerHeight;
    }
  })();

  /* ══ REVEAL: GATES, STAGES, ARCHITECTURE ══════════════════════════════ */

  (function reveals() {
    const gates = Array.from(document.querySelectorAll("#l-gates .gate"));
    if (gates.length) {
      onScreen($("l-gates"), {
        once: true,
        threshold: 0.25,
        enter: () => {
          gates.forEach((gate, i) => {
            setTimeout(() => gate.setAttribute("data-lit", "true"), reduced ? 0 : i * 260);
          });
        },
      });
    }

    // Each stage lights as it reaches the reading position, so scrolling the
    // section *is* walking the pipeline rather than watching it play.
    document.querySelectorAll("#l-stages .stage-row").forEach((row) => {
      onScreen(row, {
        once: true,
        threshold: 0.55,
        enter: () => row.setAttribute("data-lit", "true"),
      });
    });

    const arch = $("l-arch");
    if (arch) {
      // The packets travelling the architecture diagram are infinite CSS
      // animations; they run only while the diagram is actually visible.
      onScreen(arch, {
        threshold: 0.15,
        enter: () => arch.setAttribute("data-lit", "true"),
        leave: () => arch.removeAttribute("data-lit"),
      });
    }
  })();

  /* ══ BELIEF RETRACTION ════════════════════════════════════════════════
   * An explanatory visualization of the reverse walk: an action records the
   * hypothesis that justified it, so invalidating the hypothesis re-opens
   * the verdict on every unresolved action still resting on it.
   *
   * The important thing this demo shows is what does NOT happen: both
   * actions stay pending. A retraction raises a warning; it never authorizes
   * anything and never cancels anything.
   */

  (function retraction() {
    const root = $("l-retract");
    if (!root) return;

    const run = $("l-retract-run");
    const resetBtn = $("l-retract-reset");
    const status = $("l-retract-status");
    const pill = $("l-retract-pill");
    const edgeLabel = $("l-retract-edge-label");
    const deps = Array.from(root.querySelectorAll(".dep"));
    let timers = [];

    const REST = {
      1: { verdict: "MEDIUM", note: "" },
      2: { verdict: "LOW", note: "" },
    };
    const ESCALATED = {
      1: { verdict: "HIGH", note: "re-evaluated ×1 · stale_justification" },
      2: { verdict: "MEDIUM", note: "re-evaluated ×1 · stale_justification" },
    };

    const PHASES = [
      { at: 0,    text: "Telemetry reads 91%. The claim was 40%." },
      { at: 900,  text: "Theory retracted — contradicted and never re-established." },
      { at: 1800, text: "Reverse lookup finds 2 unresolved actions resting on it." },
      { at: 2700, text: "Both re-evaluated. Both still pending — nothing was authorized." },
    ];

    function paint(phase) {
      root.setAttribute("data-phase", String(phase));
      pill.textContent = phase >= 2 ? "stale" : "active";
      pill.className = phase >= 2 ? "pill pill-stale" : "pill pill-active";
      edgeLabel.textContent = phase >= 2 ? "justification invalid" : "justifies";

      deps.forEach((dep) => {
        const key = dep.dataset.dep;
        const table = phase >= 4 ? ESCALATED : REST;
        dep.querySelector(".dep-verdict").textContent = table[key].verdict;
        dep.querySelector(".dep-note").textContent = table[key].note;
        if (phase >= 4) dep.setAttribute("data-state", "escalated");
        else if (phase === 3) dep.setAttribute("data-state", "reevaluating");
        else dep.removeAttribute("data-state");
      });
    }

    function clearTimers() {
      timers.forEach(clearTimeout);
      timers = [];
    }

    run.addEventListener("click", () => {
      clearTimers();
      run.disabled = true;
      resetBtn.disabled = false;
      PHASES.forEach((phase, i) => {
        const fire = () => { paint(i + 1); status.textContent = phase.text; };
        if (reduced) fire();
        else timers.push(setTimeout(fire, phase.at));
      });
    });

    resetBtn.addEventListener("click", () => {
      clearTimers();
      paint(0);
      status.textContent = "Two actions rest on one theory.";
      run.disabled = false;
      resetBtn.disabled = true;
    });

    paint(0);
  })();

  /* ══ AMBIGUITY ════════════════════════════════════════════════════════
   * The confirmation-attribution policy, as a policy: a named target decides
   * it; a bare reply needs exactly one open action AND has to be timely.
   * Every other input is refused rather than guessed at.
   */

  (function ambiguity() {
    const root = $("l-amb");
    if (!root) return;

    const openList = $("l-amb-open");
    const traceList = $("l-amb-trace");
    const verdictBox = $("l-amb-verdict");
    const decisionNode = $("l-amb-decision");
    const countNode = $("l-amb-count");
    const replyNode = $("l-amb-reply");

    const CASES = {
      two: {
        open: [
          { text: "rollback · core-db → schema v2.3", meta: "action#7 · last live 6s ago" },
          { text: "restart · notification-service", meta: "action#9 · last live 3s ago" },
        ],
        trace: [
          ["named target in the utterance?", "no", false],
          ["exactly one open action?", "no — 2 open", false],
          ["timely?", "not reached", "skip"],
        ],
        decision: "AMBIGUOUS",
        count: "0 actions authorized",
        reply: "AEGIS asks: “Which one — the rollback of core-db, or the restart of notification-service?”",
      },
      one: {
        open: [
          { text: "rollback · core-db → schema v2.3", meta: "action#7 · last live 6s ago", resolved: true },
        ],
        trace: [
          ["named target in the utterance?", "no", false],
          ["exactly one open action?", "yes — action#7", true],
          ["timely? (last live 6s ago)", "yes", true],
        ],
        decision: "RESOLVED",
        count: "1 action authorized · attributed to uid 1001",
        reply: "Nothing spoken. The decision is written to the ledger against uid 1001, and AEGIS stops arguing about it.",
      },
      stale: {
        open: [
          { text: "rollback · core-db → schema v2.3", meta: "action#7 · last live 4m 12s ago" },
        ],
        trace: [
          ["named target in the utterance?", "no", false],
          ["exactly one open action?", "yes — action#7", true],
          ["timely? (last live 252s ago)", "no", false],
        ],
        decision: "AMBIGUOUS",
        count: "0 actions authorized",
        reply: "AEGIS asks: “Which action do you want me to confirm? The core-db rollback hasn't come up in four minutes.”",
      },
    };

    function paint(key) {
      const scenario = CASES[key];

      openList.replaceChildren(...scenario.open.map((action) => {
        const node = el("li", "dep");
        if (action.resolved && scenario.decision === "RESOLVED") {
          node.setAttribute("data-state", "escalated");
        }
        node.append(el("p", "dep-text", action.text));
        const meta = el("p", "dep-meta");
        const status = action.resolved && scenario.decision === "RESOLVED" ? "confirmed" : "pending";
        meta.append(el("span", "pill pill-" + status, status));
        meta.append(el("span", "", action.meta));
        node.append(meta);
        return node;
      }));

      traceList.replaceChildren(...scenario.trace.map(([question, answer, ok]) => {
        const row = el("li");
        row.dataset.ok = String(ok);
        row.append(el("span", "", question), el("b", "", answer));
        return row;
      }));

      verdictBox.dataset.decision = scenario.decision;
      decisionNode.textContent = scenario.decision;
      countNode.textContent = scenario.count;
      replyNode.textContent = scenario.reply;
    }

    root.querySelectorAll("[data-amb]").forEach((button) => {
      button.addEventListener("click", () => {
        root.querySelectorAll("[data-amb]").forEach((other) => {
          other.setAttribute("aria-pressed", String(other === button));
        });
        paint(button.dataset.amb);
      });
    });

    paint("two");
  })();

  /* ══ INTERVENTION GOVERNOR ════════════════════════════════════════════
   * Utility is tier plus independent findings, decayed on a 90-second
   * half-life; the open window goes to the maximum. The contrast the panel
   * exists to draw: FIFO gives the channel to whatever was mentioned first,
   * which is how a schema-breaking rollback ends up queued behind a service
   * restart somebody happened to say earlier.
   */

  (function governor() {
    const root = $("l-gov");
    if (!root) return;

    const clock = $("l-gov-clock");
    const elapsed = $("l-gov-elapsed");
    const list = $("l-gov-list");
    const fifoNode = $("l-gov-fifo");
    const pickedNode = $("l-gov-picked");
    const whyNode = $("l-gov-why");

    const HALF_LIFE = 90;
    const TIER_WEIGHT = { HIGH: 1.0, MEDIUM: 0.55, LOW: 0.2 };

    const CANDIDATES = [
      { born: 0,  tier: "MEDIUM", findings: 1,
        name: "Restart of notification-service on an unconfirmed theory",
        why: "restart_on_unconfirmed_cause" },
      { born: 12, tier: "HIGH", findings: 2,
        name: "Rollback of core-db breaks 3 services, cascading to 6",
        why: "blast_radius_schema_break + stale_justification" },
      { born: 30, tier: "MEDIUM", findings: 1,
        name: "Pool root cause contradicted and never re-established",
        why: "stale_justification" },
      { born: 88, tier: "MEDIUM", findings: 1,
        name: "Error-rate claim contradicted by telemetry",
        why: "evidence_contradiction" },
    ];

    const utility = (candidate, age) => {
      const base = TIER_WEIGHT[candidate.tier] + 0.12 * (candidate.findings - 1);
      return base * Math.pow(0.5, age / HALF_LIFE);
    };

    function paint() {
      const now = Number(clock.value);
      elapsed.textContent = "+" + now + "s";

      const live = CANDIDATES
        .filter((candidate) => now >= candidate.born)
        .map((candidate) => {
          const age = now - candidate.born;
          return { candidate, age, u: utility(candidate, age) };
        });

      if (!live.length) {
        list.replaceChildren(el("li", "gov-item", "Nothing raised yet."));
        fifoNode.textContent = "—";
        pickedNode.textContent = "—";
        return;
      }

      // Linear scan, deliberately: utility is time-varying, which breaks the
      // stable-key assumption a heap relies on.
      let best = live[0];
      live.forEach((entry) => { if (entry.u > best.u) best = entry; });
      const oldest = live.slice().sort((a, b) => b.age - a.age)[0];
      const peak = best.u;

      list.replaceChildren(...live
        .slice()
        .sort((a, b) => b.u - a.u)
        .map((entry) => {
          const item = el("li", "gov-item");
          item.dataset.tier = entry.candidate.tier;
          item.dataset.winner = String(entry === best);

          const head = el("div", "gov-item-head");
          head.append(el("span", "pill pill-" + entry.candidate.tier.toLowerCase(), entry.candidate.tier));
          head.append(el("b", "", entry.candidate.name));
          head.append(el("span", "gov-age", "raised +" + entry.candidate.born + "s · age " + entry.age + "s"));
          item.append(head);

          const track = el("div", "gov-track");
          const fill = el("i");
          fill.style.width = (Math.max(0.02, entry.u / Math.max(peak, 0.0001)) * 100).toFixed(1) + "%";
          track.append(fill);
          item.append(track);

          const meta = el("div", "gov-meta");
          meta.append(el("span", "", entry.candidate.findings + " independent finding"
            + (entry.candidate.findings === 1 ? "" : "s")));
          meta.append(el("span", "", entry.candidate.why));
          meta.append(el("span", "", "utility " + entry.u.toFixed(3)));
          item.append(meta);
          return item;
        }));

      fifoNode.textContent = oldest.candidate.name;
      pickedNode.textContent = best.candidate.name;
      whyNode.textContent = "utility " + best.u.toFixed(3)
        + " · " + best.candidate.tier
        + " · decayed " + Math.round((1 - Math.pow(0.5, best.age / HALF_LIFE)) * 100) + "% over " + best.age + "s";
    }

    clock.value = "40";
    clock.addEventListener("input", paint);
    paint();

    /* ── the 512-byte pack ────────────────────────────────────────────
     * Exact 0/1 knapsack over the *rendered* byte cost of each optional
     * sentence, with the opener, the lead finding and the closer charged
     * up front because none of them is optional. This is the same shape as
     * the server-side packer; it is running here so that the sentence on
     * screen is one that genuinely fits, rather than one that was written
     * to look like it does.
     */
    const BUDGET = 512;
    const MANDATORY = [
      { label: "opener", text: "Hold — two issues.", color: "var(--text-muted)" },
      { label: "lead · HIGH · blast_radius_schema_break",
        text: "Rollback of core-db to v2.3 will break payment-api, auth-service, and "
            + "analytics-pipeline — they're on schema v17, incompatible with v2.3, cascading to "
            + "3 more services including user-facing api-gateway.",
        color: "var(--high)" },
      { label: "closer", text: "Do you want to go ahead anyway?", color: "var(--text-muted)" },
    ];
    const OPTIONAL = [
      { label: "finding · MEDIUM · stale_justification", value: 3,
        text: "The pool root cause still isn't confirmed — it was contradicted and never re-established.",
        color: "var(--medium)" },
      { label: "finding · LOW · decision_on_record", value: 1,
        text: "And the earlier decision not to roll back is still on record from uid 1002.",
        color: "var(--text-faint)" },
      { label: "finding · MEDIUM · conflicting_proposals", value: 2,
        text: "Two people have proposed conflicting recoveries in the last ninety seconds and "
            + "neither has been confirmed by anyone in the room.",
        color: "var(--medium)" },
    ];

    // Sentences are joined by a single space, so each one costs its own bytes
    // plus the separator it brings with it. Charging the separator to the
    // item is what stops the pack overrunning by one byte per sentence.
    const cost = (part, first) => byteLength(part.text) + (first ? 0 : 1);

    function pack() {
      let spent = 0;
      MANDATORY.forEach((part, i) => { spent += cost(part, i === 0); });
      const room = BUDGET - spent;

      // Exact DP: best[c] is the highest value achievable in c bytes.
      const best = new Array(room + 1).fill(0);
      const take = OPTIONAL.map(() => new Array(room + 1).fill(false));
      OPTIONAL.forEach((part, i) => {
        const c = cost(part, false);
        for (let budget = room; budget >= c; budget -= 1) {
          const candidate = best[budget - c] + part.value;
          if (candidate > best[budget]) {
            best[budget] = candidate;
            take[i][budget] = true;
          }
        }
      });

      // Walk the table backwards to recover which sentences were chosen.
      const chosen = new Set();
      let budget = room;
      for (let i = OPTIONAL.length - 1; i >= 0; i -= 1) {
        if (take[i][budget]) {
          chosen.add(i);
          budget -= cost(OPTIONAL[i], false);
        }
      }

      const rows = [];
      MANDATORY.slice(0, 2).forEach((part, i) => rows.push({ part, kept: true, bytes: cost(part, i === 0) }));
      OPTIONAL.forEach((part, i) => rows.push({ part, kept: chosen.has(i), bytes: cost(part, false) }));
      rows.push({ part: MANDATORY[2], kept: true, bytes: cost(MANDATORY[2], false) });

      const used = rows.reduce((total, row) => total + (row.kept ? row.bytes : 0), 0);
      return { rows, used };
    }

    const packed = pack();
    $("l-gov-bytes").textContent = String(packed.used);

    const bar = $("l-gov-bar");
    const packList = $("l-gov-pack");
    bar.replaceChildren(...packed.rows.filter((row) => row.kept).map((row) => {
      const segment = document.createElement("i");
      segment.style.background = row.part.color;
      segment.style.width = (row.bytes / BUDGET * 100).toFixed(2) + "%";
      return segment;
    }));

    packList.replaceChildren(...packed.rows.map((row) => {
      const item = el("li");
      item.dataset.dropped = String(!row.kept);
      const key = el("span", "k", row.part.label + (row.kept ? "" : " · dropped"));
      key.style.color = row.kept ? row.part.color : "var(--text-faint)";
      item.append(key, el("span", "v", row.part.text), el("span", "b", row.bytes + " B"));
      return item;
    }));
  })();

  /* ══ LIVE HEALTH ══════════════════════════════════════════════════════
   * The only genuinely live thing in the narrative sections. It reads
   * /api/health, which returns names and booleans and no credential of any
   * kind. When nothing answers, the board says so rather than showing a
   * plausible-looking fixture next to copy claiming it is live.
   */

  (function health() {
    const source = $("l-health-source");
    if (!source) return;

    const set = (id, text, ok) => {
      const node = $(id);
      if (!node) return;
      node.textContent = text;
      if (ok === undefined) node.removeAttribute("data-ok");
      else node.dataset.ok = String(ok);
    };

    fetch("/api/health", { headers: { Accept: "application/json" } })
      .then((response) => (response.ok ? response.json() : null))
      .then((health) => {
        if (!health) throw new Error("no health payload");
        set("l-health-provider", health.extraction_provider || "unknown");
        set("l-health-agora", health.agora_authenticated ? "configured" : "not configured",
            Boolean(health.agora_authenticated));
        set("l-health-window", health.window_open ? "open" : "cooling",
            Boolean(health.window_open));
        set("l-health-rate", (health.rate_limit_seconds != null ? health.rate_limit_seconds : "—") + " s");
        source.textContent = "read from /api/health on this server";
        source.dataset.live = "true";
      })
      .catch(() => {
        source.textContent = "no backend answered — showing nothing rather than inventing it";
        source.dataset.live = "false";
      });
  })();
})();
