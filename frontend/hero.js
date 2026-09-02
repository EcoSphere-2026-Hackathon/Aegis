/* AEGIS landing walkthrough — the filmed console section of the landing page.
 *
 * Vanilla, same reasoning as app.js: this page is served by the Python
 * backend off /static and has to run on a demo laptop with no toolchain.
 *
 * This file owns the stage, its transport and the topology explorer.
 * landing.js owns every other section of the page; the two share nothing but
 * the DOM and are safe to load in either order.
 *
 * What this file is, precisely: a *deterministic replay* of the rehearsed
 * golden demo (data/transcripts/golden_demo.json) rendered into the real
 * console markup. It is a replay and says so — the transport reads
 * "rehearsed demo", and nothing here claims to be a live incident. The
 * shapes it renders are the shapes the API returns, and the parts that can
 * be read from a running backend are read from it rather than hardcoded:
 *
 *   GET /api/topology  -> the dependency graph the blast radius traverses
 *   GET /api/telemetry -> metric names, units and current values
 *   GET /api/metrics   -> the latency figures quoted in the engineering strip
 *   GET /api/health    -> which extractor is actually in use
 *
 * When the page is opened without a backend (file://, static hosting) the
 * shipped fixture below stands in, and `sourceNote` still says the run is
 * rehearsed. There is no path in this file that invents a capability: every
 * sentence AEGIS "speaks" is composed the way backend/governor/speech.py
 * composes it, from findings the risk engine would produce.
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
  const clamp = (v, lo, hi) => (v < lo ? lo : v > hi ? hi : v);
  const byteLength = (text) => new TextEncoder().encode(text).length;
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* ── the shipped fixture ─────────────────────────────────────────────
   * Mirrors backend/risk_engine/topology.py build_incident_topology() and
   * backend/telemetry/mock_telemetry.py. Used only until /api answers.
   */
  const FIXTURE = {
    topology: {
      nodes: ["analytics-pipeline", "api-gateway", "auth-service", "billing-service", "cache-layer",
              "core-db", "notification-service", "payment-api", "search-index", "user-service"],
      edges: [
        { from: "payment-api", to: "core-db", type: "depends_on" },
        { from: "auth-service", to: "core-db", type: "depends_on" },
        { from: "cache-layer", to: "core-db", type: "depends_on" },
        { from: "analytics-pipeline", to: "core-db", type: "depends_on" },
        { from: "billing-service", to: "payment-api", type: "depends_on" },
        { from: "notification-service", to: "billing-service", type: "depends_on" },
        { from: "api-gateway", to: "auth-service", type: "depends_on" },
        { from: "api-gateway", to: "payment-api", type: "depends_on" },
        { from: "search-index", to: "analytics-pipeline", type: "depends_on" },
        { from: "user-service", to: "auth-service", type: "depends_on" },
        { from: "payment-api", to: "core-db", type: "reads_schema", schema_version: "v17" },
        { from: "auth-service", to: "core-db", type: "reads_schema", schema_version: "v17" },
        { from: "cache-layer", to: "core-db", type: "reads_schema", schema_version: "v17" },
        { from: "cache-layer", to: "core-db", type: "compatible_with", compatible_versions: ["v17", "v2.3"] },
      ],
    },
    telemetry: [
      { name: "pool_utilization", current_value: 91, unit: "%" },
      { name: "error_rate", current_value: 12.4, unit: "%" },
      { name: "p99_latency", current_value: 2400, unit: "ms" },
      { name: "schema_version", current_value: "v17", unit: null },
    ],
  };

  const TARGET = "core-db";
  const CURRENT_SCHEMA = "v17";
  const ROLLBACK_SCHEMA = "v2.3";

  /* ── graph reasoning, mirroring risk_engine/topology.py ─────────────── */

  function indexTopology(topology) {
    const reverse = {};
    (topology.nodes || []).forEach((n) => { reverse[n] = []; });
    const dependsOn = [];
    const readsSchema = {};
    const tolerant = {};
    (topology.edges || []).forEach((e) => {
      if (e.type === "depends_on") {
        dependsOn.push({ a: e.from, b: e.to });
        if (reverse[e.to]) reverse[e.to].push(e.from);
      } else if (e.type === "reads_schema") {
        readsSchema[e.from + "|" + e.to] = e.schema_version || CURRENT_SCHEMA;
      } else if (e.type === "compatible_with") {
        tolerant[e.from + "|" + e.to] = e.compatible_versions || [];
      }
    });
    Object.keys(reverse).forEach((k) => reverse[k].sort());
    return { reverse, dependsOn, readsSchema, tolerant, nodes: (topology.nodes || []).slice() };
  }

  /* Shortest reverse path per affected node — the *path* is part of the
   * product: "rolling back core-db breaks payment-api" is only credible if
   * the system can say how it knows. */
  function blastPaths(graph, node) {
    const predecessor = { [node]: null };
    const order = [];
    const queue = [node];
    while (queue.length) {
      const cur = queue.shift();
      (graph.reverse[cur] || []).forEach((dep) => {
        if (dep in predecessor) return;
        predecessor[dep] = cur;
        order.push(dep);
        queue.push(dep);
      });
    }
    return order.map((affected) => {
      const chain = [];
      let cursor = affected;
      while (cursor !== null && cursor !== undefined) { chain.push(cursor); cursor = predecessor[cursor]; }
      return chain;
    }).sort((a, b) => (a.length - b.length) || a[0].localeCompare(b[0]));
  }

  /* Multi-source reverse BFS over depends_on — direct breakage, the cascade
   * behind it, and the entry points the cascade reaches. */
  function propagate(graph, broken) {
    const seen = new Set(broken);
    const queue = broken.slice();
    const transitive = [];
    const depth = {};
    broken.forEach((n) => { depth[n] = 1; });
    while (queue.length) {
      const cur = queue.shift();
      (graph.reverse[cur] || []).forEach((dep) => {
        if (seen.has(dep)) return;
        seen.add(dep);
        depth[dep] = depth[cur] + 1;
        transitive.push(dep);
        queue.push(dep);
      });
    }
    const affected = Array.from(seen);
    return {
      direct: broken.slice().sort(),
      transitive: transitive.slice().sort(),
      entry: affected.filter((n) => !(graph.reverse[n] || []).length).sort(),
      total: affected.length,
      depth,
    };
  }

  /* Which dependents a rollback actually breaks: reads the schema, is not
   * declared compatible across the change. cache-layer is the control. */
  function schemaBreakage(graph) {
    return (graph.reverse[TARGET] || []).filter((dep) => {
      const required = graph.readsSchema[dep + "|" + TARGET];
      if (!required || required === ROLLBACK_SCHEMA) return false;
      const declared = graph.tolerant[dep + "|" + TARGET] || [];
      return !(declared.indexOf(required) >= 0 && declared.indexOf(ROLLBACK_SCHEMA) >= 0);
    }).sort();
  }

  const joinNames = (names) =>
    names.length <= 1 ? (names[0] || "none")
    : names.length === 2 ? names[0] + " and " + names[1]
    : names.slice(0, -1).join(", ") + ", and " + names[names.length - 1];

  /* ── runtime context ────────────────────────────────────────────────── */

  const ctx = {
    graph: indexTopology(FIXTURE.topology),
    metrics: FIXTURE.telemetry,
    live: false,
    findings: null,
    speech: null,
    blast: null,
  };

  function recompute() {
    const broken = schemaBreakage(ctx.graph);
    const prop = propagate(ctx.graph, broken.length ? broken : []);
    ctx.blast = { broken, prop };

    // risk_engine/checks.py check 3, verbatim sentence shape.
    let blastMessage = "Rollback of " + TARGET + " to " + ROLLBACK_SCHEMA + " will break "
      + joinNames(broken) + " — they're on schema " + CURRENT_SCHEMA + ", incompatible with "
      + ROLLBACK_SCHEMA;
    if (prop.transitive.length) {
      blastMessage += ", cascading to " + prop.transitive.length + " more service"
        + (prop.transitive.length === 1 ? "" : "s");
      if (prop.entry.length) {
        blastMessage += " including user-facing " + joinNames(prop.entry.slice(0, 2));
      }
    }

    const pool = ctx.metrics.filter((m) => m.name === "pool_utilization")[0];
    const measured = pool ? pool.current_value : 91;

    ctx.findings = {
      contradiction: "telemetry shows pool utilization at " + measured + "%, not 40%",
      blast: blastMessage,
      stale: "The pool root cause still isn't confirmed — it was contradicted and never re-established.",
      measured,
    };

    // governor/speech.py: opener, findings worst-first, closer that hands the
    // decision back. The compound one is measured against the 512-byte cap.
    const opener = "Hold — two issues.";
    const closer = "Do you want to go ahead anyway?";
    const compound = opener + " " + blastMessage + ". " + ctx.findings.stale + " " + closer;
    ctx.speech = {
      first: "Hold — " + ctx.findings.contradiction + ". Want to re-check before ruling it out?",
      compound,
      compoundParts: [
        { label: "opener", text: opener, color: "var(--text-muted)" },
        { label: "finding · HIGH · blast_radius_schema_break", text: blastMessage + ".", color: "var(--high)" },
        { label: "finding · MEDIUM · stale_justification", text: ctx.findings.stale, color: "var(--medium)" },
        { label: "closer", text: closer, color: "var(--text-muted)" },
      ],
      compoundBytes: byteLength(compound),
      status: "Status. 1 theory still open and unconfirmed: it is the pool then; decisions on record: "
        + "don't rollback, let's check the pool metrics properly first; nothing awaiting a decision.",
    };
  }
  recompute();

  /* ── the timeline ───────────────────────────────────────────────────── */

  const CUE = {
    standby: 0, firstTurn: 4.5, claim: 9.5, grounding: 15.5, intervene1: 22.5,
    rollback: 29, blast: 35, stale: 44, budget: 49.5, intervene2: 54,
    human: 60.5, status: 66.5,
  };
  const TOTAL = 73.5;

  const BEATS = [
    { at: CUE.standby, label: "Standby" },
    { at: CUE.firstTurn, label: "First turn" },
    { at: CUE.claim, label: "The claim" },
    { at: CUE.grounding, label: "Grounding" },
    { at: CUE.intervene1, label: "It speaks" },
    { at: CUE.rollback, label: "Rollback proposed" },
    { at: CUE.blast, label: "Blast radius" },
    { at: CUE.stale, label: "Belief retraction" },
    { at: CUE.budget, label: "512 bytes" },
    { at: CUE.intervene2, label: "Compound catch" },
    { at: CUE.human, label: "Human decides" },
    { at: CUE.status, label: "Status" },
  ];

  // Transcript verbatim from data/transcripts/golden_demo.json.
  const TURNS = () => [
    { at: CUE.firstTurn + 0.3, uid: "1001", who: "Engineer A",
      text: "Payments are throwing 500s, seeing timeouts.",
      claims: [{ at: CUE.firstTurn + 0.9, type: "fact" }] },
    { at: CUE.claim + 0.4, uid: "1002", who: "Engineer B",
      text: "Pool utilization looks fine, like 40%.",
      claims: [{ at: CUE.claim + 1.0, type: "hypothesis", metric: "pool_utilization" }] },
    { at: CUE.rollback + 0.3, uid: "1001", who: "Engineer A",
      text: "Okay, fine, it is the pool then. Let's rollback Core to the last version.",
      claims: [{ at: CUE.rollback + 0.9, type: "hypothesis", metric: "pool_utilization" },
               { at: CUE.rollback + 1.1, type: "proposed_action", target: TARGET }] },
    { at: CUE.human + 0.4, uid: "1002", who: "Engineer B",
      text: "Hold — don't rollback, let's check the pool metrics properly first.",
      claims: [{ at: CUE.human + 1.0, type: "hold", target: TARGET }] },
    { at: CUE.status + 0.3, uid: "1001", who: "Engineer A", text: "AEGIS, status?", claims: [] },
  ];

  const INTERVENTIONS = () => [
    { at: CUE.firstTurn + 1.9, tier: "", spoken: false, outcome: "no action needed",
      text: "Ordinary incident chatter. Nothing to say.", reasons: [] },
    { at: CUE.intervene1 + 0.6, tier: "HIGH", spoken: true, outcome: "spoken over the call",
      text: ctx.speech.first,
      reasons: ["evidence_contradiction — " + ctx.findings.contradiction] },
    { at: CUE.intervene2 + 0.6, tier: "HIGH", spoken: true, outcome: "spoken over the call",
      text: ctx.speech.compound,
      reasons: ["blast_radius_schema_break — " + joinNames(ctx.blast.broken)
                  + " read " + TARGET + " schema " + CURRENT_SCHEMA,
                "stale_justification — the pool root cause was contradicted and never re-established"] },
    { at: CUE.status + 2.5, tier: "", spoken: true, outcome: "status summary, on request",
      text: ctx.speech.status, reasons: [] },
  ];

  const CAPTIONS = [
    { at: 3.4, until: CUE.firstTurn + 0.2, text: "Two engineers on a live bridge. AEGIS is on the call, and nobody talks to it." },
    { at: CUE.firstTurn + 1.4, until: CUE.claim, text: "Ordinary chatter. It says nothing — that is the product working." },
    { at: CUE.claim + 1.4, until: CUE.grounding + 0.6, text: "A number recited from impression." },
    { at: CUE.grounding + 1.2, until: CUE.intervene1, text: "Telemetry reads 91%. The model produced the claim; Python found the contradiction." },
    { at: CUE.intervene1 + 1.4, until: CUE.rollback, text: "The first thing it has said all call." },
    { at: CUE.rollback + 1.6, until: CUE.blast + 0.4, text: "Now a destructive action, resting on a theory nobody confirmed." },
    { at: CUE.blast + 1.4, until: CUE.blast + 4.2, text: "Direct dependents are not the blast radius." },
    { at: CUE.blast + 4.6, until: CUE.stale + 0.4, text: "Six services, three of them user-facing. Nobody said “v2.3” — that came from the topology." },
    { at: CUE.stale + 1.6, until: CUE.budget, text: "Retracting a belief re-opens everything that rested on it." },
    { at: CUE.budget + 0.8, until: CUE.intervene2 + 0.4, text: "One channel, 512 bytes. What gets said is packed exactly, not truncated." },
    { at: CUE.intervene2 + 1.4, until: CUE.human + 0.4, text: "Two findings, one interruption." },
    { at: CUE.human + 1.8, until: CUE.status + 0.4, text: "The human decides. AEGIS records who held it and stops arguing." },
    { at: CUE.status + 1.4, until: TOTAL - 2.8, text: "Open theories are voiced as unconfirmed. Speaking a hypothesis as settled fact is a hard fail." },
  ];

  /* Camera, in film coordinates (1440×810). Each entry is "by this time, be
   * looking here": the move runs over the 1.4s after the cue, so a beat
   * boundary is a move rather than a cut. */
  const CAMERA = [
    [0, 720, 405, 1.0], [CUE.standby + 2.6, 720, 405, 1.05],
    [CUE.firstTurn + 0.2, 300, 330, 1.42], [CUE.claim + 0.4, 300, 380, 1.5],
    [CUE.grounding + 0.3, 900, 470, 1.15], [CUE.intervene1 + 0.2, 1160, 300, 1.4],
    [CUE.rollback + 0.2, 320, 420, 1.45], [CUE.blast + 0.3, 720, 405, 1.0],
    [CUE.stale + 0.2, 720, 405, 1.02], [CUE.budget + 0.2, 720, 405, 1.02],
    [CUE.intervene2 + 0.2, 1160, 320, 1.35], [CUE.human + 0.3, 780, 470, 1.4],
    [CUE.status + 0.4, 720, 405, 1.05], [TOTAL - 1.2, 720, 405, 1.0],
  ];

  const SURFACES = [
    { node: "s-contradiction", from: CUE.grounding + 0.2, to: CUE.intervene1 + 1.4 },
    { node: "s-blast", from: CUE.blast, to: CUE.stale + 0.2 },
    { node: "s-retraction", from: CUE.stale + 0.3, to: CUE.budget + 0.1 },
    { node: "s-budget", from: CUE.budget + 0.15, to: CUE.intervene2 + 1.0 },
  ];

  /* ── easing ─────────────────────────────────────────────────────────── */

  const easeInOut = (p) => (p < 0.5 ? 4 * p * p * p : 1 - Math.pow(-2 * p + 2, 3) / 2);
  const ramp = (t, start, end) => clamp((t - start) / Math.max(0.0001, end - start), 0, 1);
  const eased = (t, start, end) => easeInOut(ramp(t, start, end));

  /* ── film DOM ───────────────────────────────────────────────────────── */

  const film = {
    root: $("film"),
    conversation: $("f-conversation"),
    turnCount: $("f-turn-count"),
    interventions: $("f-interventions"),
    interventionsCount: $("f-interventions-count"),
    metrics: $("f-metrics"),
    evidenceCount: $("f-evidence-count"),
    listening: $("f-listening"),
    windowChip: $("f-window"),
    windowValue: $("f-window-value"),
    provider: $("f-provider"),
    groups: {
      hypotheses: { list: $("f-hypotheses"), count: $("f-hypotheses-count"), empty: $("f-hypotheses-empty") },
      facts: { list: $("f-facts"), count: $("f-facts-count"), empty: $("f-facts-empty") },
      decisions: { list: $("f-decisions"), count: $("f-decisions-count"), empty: $("f-decisions-empty") },
      actions: { list: $("f-actions"), count: $("f-actions-count"), empty: $("f-actions-empty") },
    },
  };

  const pill = (text, kind) => el("span", "pill pill-" + kind, text);

  function emptyBlock(title, body) {
    const wrap = el("div", "empty");
    wrap.append(el("p", "empty-title", title), el("p", "empty-body", body));
    return wrap;
  }

  /* render(t) rebuilds the film from the timeline rather than applying
   * incremental ops, so scrubbing backwards lands on exactly the state that
   * playing forwards would produce. The tree is a few dozen nodes; a rebuild
   * is cheaper than reconciling two directions of travel. */
  function renderFilm(t) {
    const turns = TURNS().filter((turn) => t >= turn.at);
    film.conversation.replaceChildren(
      ...(turns.length ? turns.map((turn) => {
        const node = el("article", "turn");
        const head = el("div", "turn-head");
        head.append(el("span", "speaker", "uid " + turn.uid));
        head.append(el("span", "", turn.who));
        node.append(head, el("p", "turn-text", turn.text));
        const claims = el("div", "claims");
        turn.claims.filter((c) => t >= c.at).forEach((claim) => {
          const tag = el("span", "claim-tag", claim.type.replace(/_/g, " "));
          tag.dataset.type = claim.type;
          claims.append(tag);
        });
        node.append(claims);
        return node;
      }) : [emptyBlock("Nothing said yet",
            "Transcripts appear here as people speak. AEGIS stays silent unless something needs saying.")])
    );
    film.turnCount.textContent = turns.length + (turns.length === 1 ? " turn" : " turns");

    const shown = INTERVENTIONS().filter((iv) => t >= iv.at).reverse();   // newest first: the
    // payoff card must not be the one that falls off the bottom of a panel
    // the film cannot scroll.
    film.interventions.replaceChildren(
      ...(shown.length ? shown.map((iv) => {
        const card = el("article", "intervention");
        card.dataset.tier = iv.tier;
        card.dataset.spoken = String(iv.spoken);
        const head = el("div", "intervention-head");
        head.append(el("span", "tier", iv.tier || "STATUS"));
        head.append(el("span", "outcome", iv.outcome));
        card.append(head, el("p", "spoken", iv.text));
        if (iv.reasons.length) {
          const list = el("ul", "reasons");
          iv.reasons.forEach((reason) => list.append(el("li", "", reason)));
          card.append(list);
        }
        return card;
      }) : [emptyBlock("Silent",
            "Every intervention AEGIS makes is recorded here with the exact rule it caught.")])
    );
    film.interventionsCount.textContent = shown.filter((iv) => iv.spoken).length;

    /* state panel */
    const theories = [
      { at: CUE.claim + 1.1, text: "Pool utilization is fine, ~40% — core-db", uid: "1002", staleAt: CUE.grounding + 1.4 },
      { at: CUE.rollback + 1.2, text: "It is the pool — core-db", uid: "1001", staleAt: CUE.stale + 1.1 },
    ].filter((h) => t >= h.at);

    fill(film.groups.hypotheses, theories, (h) => {
      const stale = t >= h.staleAt;
      const node = el("li", "item");
      node.dataset.stale = String(stale);
      node.append(el("p", "item-text", h.text));
      const meta = el("div", "item-meta");
      meta.append(pill(stale ? "stale" : "active", stale ? "stale" : "active"));
      meta.append(el("span", "", "pool_utilization"));
      meta.append(el("span", "", "uid " + h.uid));
      if (stale) {
        const why = el("span", "", "contradicted by telemetry");
        why.style.color = "var(--high)";
        meta.append(why);
      }
      node.append(meta);
      return node;
    });

    fill(film.groups.facts, t >= CUE.firstTurn + 1.2
      ? [{ text: "Payment-api returning 500s and timeouts" }] : [], (f) => {
      const node = el("li", "item");
      node.append(el("p", "item-text", f.text));
      const meta = el("div", "item-meta");
      meta.append(pill("corroborated", "active"));
      meta.append(el("span", "", "error_rate 12.4%"));
      meta.append(el("span", "", "uid 1001"));
      node.append(meta);
      return node;
    });

    fill(film.groups.decisions, t >= CUE.human + 1.6
      ? [{ text: "Don't rollback — check the pool metrics properly first" }] : [], (d) => {
      const node = el("li", "item");
      node.append(el("p", "item-text", d.text));
      const meta = el("div", "item-meta");
      meta.append(pill("hold", "held"));
      meta.append(el("span", "", TARGET));
      meta.append(el("span", "", "uid 1002"));
      node.append(meta);
      return node;
    });

    fill(film.groups.actions, t >= CUE.rollback + 1.5 ? [{}] : [], () => {
      const held = t >= CUE.human + 1.6;
      const evaluated = t >= CUE.blast + 3;
      const node = el("li", "item");
      node.append(el("p", "item-text", "rollback · " + TARGET + " → schema " + ROLLBACK_SCHEMA));
      const meta = el("div", "item-meta");
      meta.append(pill(held ? "held" : "pending", held ? "held" : "pending"));
      meta.append(evaluated ? pill("HIGH", "high") : pill("evaluating", "low"));
      meta.append(el("span", "", "justified by: it is the pool"));
      if (t >= CUE.stale + 1.6) {
        const again = el("span", "", "re-evaluated ×1");
        again.style.color = "var(--medium)";
        meta.append(again);
      }
      node.append(meta);
      return node;
    });

    /* evidence */
    const contested = t >= CUE.grounding + 0.9 && t < CUE.human;
    film.metrics.replaceChildren(...ctx.metrics.map((metric) => {
      const node = el("li", "metric");
      node.dataset.contested = String(contested && metric.name === "pool_utilization");
      node.append(el("span", "metric-name", metric.name));
      const value = metric.unit === "%" ? metric.current_value + "%"
        : metric.unit ? metric.current_value + " " + metric.unit
        : String(metric.current_value);
      node.append(el("span", "metric-value", value));
      return node;
    }));
    film.evidenceCount.textContent = ctx.metrics.length;

    /* chrome */
    const speaking = (t > CUE.intervene1 + 0.5 && t < CUE.intervene1 + 4.2)
      || (t > CUE.intervene2 + 0.5 && t < CUE.intervene2 + 5.2)
      || (t > CUE.status + 2.4 && t < CUE.status + 5.2);
    const listening = t > CUE.firstTurn;
    film.listening.dataset.state = speaking ? "speaking" : listening ? "active" : "idle";
    film.listening.querySelector(".status-label").textContent = speaking ? "Intervening" : "Listening";

    // One intervention per 45s window: after it speaks, the window is shut
    // and the countdown is the reason the next catch waits.
    let windowState = "open", windowText = "open";
    const closes = [[CUE.intervene1 + 0.8, CUE.rollback - 0.4], [CUE.intervene2 + 0.8, CUE.status + 2.2]];
    closes.forEach(([from, to]) => {
      if (t > from && t < to) {
        windowState = "closed";
        windowText = "closed · " + Math.max(0, Math.round(45 * (1 - (t - from) / (to - from)))) + "s";
      }
    });
    film.windowChip.dataset.state = windowState;
    film.windowValue.textContent = windowText;

    // The real console pins each stream to its newest entry (app.js
    // scrollToEnd); the film has to do the same or a late turn is clipped.
    film.conversation.scrollTop = film.conversation.scrollHeight;
    film.interventions.scrollTop = 0;
  }

  function fill(group, items, render) {
    group.list.replaceChildren(...items.map(render));
    group.count.textContent = items.length;
    group.empty.hidden = items.length > 0;
  }

  /* ── camera ─────────────────────────────────────────────────────────── */

  const stage = $("stage");

  function applyCamera(t) {
    const rect = stage.getBoundingClientRect();
    const base = rect.width / 1440;
    let pose = [CAMERA[0][1], CAMERA[0][2], CAMERA[0][3]];
    if (!reduced) {
      for (let i = 1; i < CAMERA.length; i++) {
        const k = CAMERA[i];
        if (t < k[0]) break;
        const e = eased(t, k[0], k[0] + 1.4);
        pose = [pose[0] + (k[1] - pose[0]) * e, pose[1] + (k[2] - pose[1]) * e, pose[2] + (k[3] - pose[2]) * e];
      }
    }
    const scale = base * pose[2];
    // Clamp so the console's own edges never enter frame: a black gutter
    // reads as a broken layout, not as a camera move.
    const tx = clamp(rect.width / 2 - pose[0] * scale, rect.width - 1440 * scale, 0);
    const ty = clamp(rect.height / 2 - pose[1] * scale, rect.height - 810 * scale, 0);
    film.root.style.transform = "translate(" + tx.toFixed(2) + "px," + ty.toFixed(2) + "px) scale("
      + scale.toFixed(4) + ")";
  }

  /* ── surfaces ───────────────────────────────────────────────────────── */

  const surfaceNodes = {};
  SURFACES.forEach((s) => { surfaceNodes[s.node] = $(s.node); });

  // Blast graph: laid out from the topology itself (column per hop depth) so
  // it follows /api/topology rather than a hardcoded picture.
  function layoutBlast() {
    const canvas = $("s-blast-canvas");
    const svg = $("s-blast-edges");
    const prop = propagate(ctx.graph, [TARGET]);
    const columns = {};
    ctx.graph.nodes.forEach((n) => {
      const d = n === TARGET ? 0 : (prop.depth[n] || 1) - 1;
      (columns[d] = columns[d] || []).push(n);
    });
    const depths = Object.keys(columns).map(Number).sort((a, b) => a - b);
    const positions = {};
    depths.forEach((d, di) => {
      const column = columns[d].slice().sort();
      column.forEach((n, i) => {
        positions[n] = {
          x: 7 + (di / Math.max(1, depths.length - 1)) * 82,
          y: column.length === 1 ? 50 : 12 + (i / (column.length - 1)) * 76,
        };
      });
    });

    canvas.querySelectorAll(".gnode").forEach((n) => n.remove());
    svg.replaceChildren();

    ctx.graph.dependsOn.forEach((edge) => {
      const a = positions[edge.a], b = positions[edge.b];
      if (!a || !b) return;
      const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
      line.setAttribute("x1", (b.x / 100 * 1200).toFixed(1));
      line.setAttribute("y1", (b.y / 100 * 500).toFixed(1));
      line.setAttribute("x2", (a.x / 100 * 1200).toFixed(1));
      line.setAttribute("y2", (a.y / 100 * 500).toFixed(1));
      line.setAttribute("stroke", "#1e2430");
      line.setAttribute("stroke-width", "1.6");
      line.dataset.dependent = edge.a;
      svg.append(line);
    });

    ctx.graph.nodes.forEach((n) => {
      const p = positions[n];
      if (!p) return;
      const node = el("div", "gnode");
      node.style.left = p.x + "%";
      node.style.top = p.y + "%";
      node.dataset.node = n;
      node.dataset.role = roleOf(n);
      node.append(el("span", "n", n), el("span", "s", labelOf(n)));
      canvas.append(node);
    });
  }

  function roleOf(node) {
    if (node === TARGET) return "changed";
    if (ctx.blast.broken.indexOf(node) >= 0) return "direct";
    if (ctx.blast.prop.transitive.indexOf(node) >= 0) return "cascade";
    const declared = ctx.graph.tolerant[node + "|" + TARGET];
    if (declared && declared.length) return "tolerant";
    return "idle";
  }

  function labelOf(node) {
    const role = roleOf(node);
    if (role === "changed") return "changed";
    if (role === "direct") return "breaks · schema " + CURRENT_SCHEMA;
    if (role === "cascade") return ctx.blast.prop.entry.indexOf(node) >= 0 ? "user-facing" : "cascade";
    if (role === "tolerant") return "compatible " + CURRENT_SCHEMA + " · " + ROLLBACK_SCHEMA;
    return "not affected";
  }

  function renderSurfaces(t) {
    SURFACES.forEach((s) => {
      surfaceNodes[s.node].dataset.live = String(t >= s.from && t < s.to);
    });

    /* contradiction */
    const g0 = CUE.grounding + 0.2;
    $("s-claimed-fill").style.width = (40 * eased(t, g0 + 0.5, g0 + 1.9)) + "%";
    $("s-measured-fill").style.width = (ctx.findings.measured * eased(t, g0 + 0.85, g0 + 2.25)) + "%";
    $("s-measured").textContent = ctx.findings.measured + "%";
    $("s-contradiction-note").style.opacity = ramp(t, g0 + 2.1, g0 + 2.6);

    /* blast radius: one hop per 0.85s, so the cascade reads as travel */
    const b0 = CUE.blast;
    $("s-blast-canvas").querySelectorAll(".gnode").forEach((node) => {
      const name = node.dataset.node;
      const depth = name === TARGET ? 0 : (ctx.blast.prop.depth[name] || 0);
      const lit = name === TARGET ? t >= b0 + 0.5
        : depth ? t >= b0 + 1.2 + (depth - 1) * 0.85
        : t >= b0 + 3.4;
      node.dataset.lit = String(lit);
    });
    $("s-blast-edges").querySelectorAll("line").forEach((line) => {
      const dep = line.dataset.dependent;
      const depth = ctx.blast.prop.depth[dep] || 0;
      const lit = depth && t >= b0 + 1.2 + (depth - 1) * 0.85;
      line.setAttribute("stroke", lit ? (ctx.blast.broken.indexOf(dep) >= 0 ? "#ff5c5c" : "#ffb454") : "#1e2430");
      line.setAttribute("stroke-width", lit ? "2.6" : "1.6");
    });
    const countsLive = t >= b0 + 4.4;
    $("s-blast-counts").dataset.live = String(countsLive);
    $("s-count-direct").textContent = ctx.blast.prop.direct.length;
    $("s-count-cascade").textContent = ctx.blast.prop.transitive.length;
    $("s-count-total").textContent = ctx.blast.prop.total;
    $("s-count-entry").textContent = ctx.blast.prop.entry.length;

    /* belief retraction */
    const r0 = CUE.stale + 0.3;
    const broken = t >= r0 + 1.9;
    $("s-chain-hypothesis").dataset.void = String(broken);
    $("s-chain-edge").dataset.broken = String(broken);
    $("s-chain-label").textContent = broken ? "justification invalid" : "justifies";
    $("s-chain-action").dataset.reeval = String(t >= r0 + 3.1);
    $("s-chain-verdict").dataset.live = String(t >= r0 + 2.8);

    /* speech budget */
    const u0 = CUE.budget + 0.15;
    const rows = $("s-budget-rows");
    const bar = $("s-budget-bar");
    if (!rows.childElementCount) {
      ctx.speech.compoundParts.forEach((part) => {
        const row = el("div", "budget-row");
        row.style.borderLeftColor = part.color;
        const k = el("span", "k", part.label);
        k.style.color = part.color;
        row.append(k, el("span", "v", part.text), el("span", "b", byteLength(part.text) + " B"));
        rows.append(row);

        const seg = document.createElement("i");
        seg.style.background = part.color;
        seg.dataset.cost = String(byteLength(part.text + " "));
        bar.append(seg);
      });
    }
    const offsets = [0.4, 0.9, 1.7, 2.3];
    Array.prototype.forEach.call(rows.children, (row, i) => {
      row.dataset.live = String(t >= u0 + offsets[i]);
    });
    Array.prototype.forEach.call(bar.children, (seg, i) => {
      const p = eased(t, u0 + offsets[i], u0 + offsets[i] + 0.5);
      seg.style.width = (Number(seg.dataset.cost) / 512 * 100 * p) + "%";
    });
    $("s-budget-used").textContent = Math.round(ctx.speech.compoundBytes * eased(t, u0 + 0.45, u0 + 2.75));
  }

  /* ── captions, veil, lockups ────────────────────────────────────────── */

  const captionNode = $("caption");
  let captionShown = null;

  function renderChrome(t) {
    const active = CAPTIONS.filter((c) => t >= c.at && t < c.until).pop();
    const text = active ? active.text : "";
    if (text !== captionShown) {
      captionShown = text;
      captionNode.textContent = text;
      captionNode.style.opacity = text ? "1" : "0";
    }

    const open = eased(t, 0.9, 2.2);
    const close = 1 - eased(t, TOTAL - 1.4, TOTAL - 0.15);
    $("stage-veil").style.opacity = String(1 - Math.min(open, close));
    $("lockup-open").style.opacity = String(Math.min(ramp(t, 0.15, 1.05), 1 - eased(t, 3.1, 4.1)));
    $("lockup-end").style.opacity = String(ramp(t, TOTAL - 2.6, TOTAL - 1.7));
    $("lockup-end").style.pointerEvents = t > TOTAL - 2.2 ? "auto" : "none";
  }

  /* ── transport ──────────────────────────────────────────────────────── */

  const transport = { t: 0, playing: false, raf: 0, last: 0, lastTick: 0 };
  // Visibility drives play/pause now that the stage sits in the middle of the
  // page rather than at the top of it -- but an explicit press of the
  // transport wins over it. A viewer who paused to read a caption should not
  // have the film start again under them because they scrolled two pixels.
  let userPaused = false;
  let stageVisible = false;
  const playBtn = $("play");
  const scrub = $("scrub");
  const clockNode = $("clock");
  const rail = $("beat-rail");

  const mmss = (s) => Math.floor(s / 60) + ":" + String(Math.floor(s % 60)).padStart(2, "0");

  BEATS.forEach((beat) => {
    const button = el("button", "beat", beat.label);
    button.type = "button";
    button.setAttribute("role", "tab");
    button.addEventListener("click", () => seek(beat.at + 0.05, { pause: false }));
    beat.node = button;
    rail.append(button);
  });

  function render(t) {
    renderFilm(t);
    renderSurfaces(t);
    renderChrome(t);
    applyCamera(t);
    scrub.value = String(Math.round((t / TOTAL) * 1000));
    clockNode.textContent = mmss(t) + " / " + mmss(TOTAL);
    let current = BEATS[0];
    BEATS.forEach((beat) => { if (t >= beat.at) current = beat; });
    // Only when it changes. render() runs every animation frame, and writing
    // an attribute on every beat sixty times a second is a style
    // invalidation per beat per frame for a value that changes nine times in
    // the whole walkthrough.
    if (current === transport.currentBeat) return;
    transport.currentBeat = current;
    BEATS.forEach((beat) => {
      beat.node.setAttribute("aria-current", String(beat === current));
    });
  }

  function seek(t, options) {
    transport.t = clamp(t, 0, TOTAL);
    if (options && options.pause) setPlaying(false);
    render(transport.t);
  }

  function setPlaying(next) {
    transport.playing = next;
    playBtn.textContent = next ? "Pause" : "Play";
    playBtn.setAttribute("aria-label", next ? "Pause walkthrough" : "Play walkthrough");
    transport.last = now();
  }

  function now() {
    return window.performance ? window.performance.now() : Date.now();
  }

  function step(stamp) {
    if (!transport.playing) { transport.last = stamp; return; }
    const dt = Math.min(0.25, (stamp - transport.last) / 1000);
    transport.last = stamp;
    transport.t += dt;
    if (transport.t >= TOTAL) transport.t = 0;      // loops on black, both ends
    render(transport.t);
  }

  /* rAF is the clock, with a 24Hz guard behind it: rAF is suspended in
   * background tabs and in some embedded viewers, and a walkthrough whose
   * clock never advances would sit on the opening black frame — the exact
   * blank-screen failure this page exists to avoid. */
  function startClock() {
    const rafLoop = (stamp) => {
      transport.lastTick = stamp;
      step(stamp);
      transport.raf = requestAnimationFrame(rafLoop);
    };
    transport.raf = requestAnimationFrame(rafLoop);
    setInterval(() => {
      const stamp = now();
      if (stamp - (transport.lastTick || 0) > 260) {
        transport.lastTick = stamp;
        step(stamp);
      }
    }, 42);
  }

  /* Any deliberate interaction latches the transport: from here on the film
   * does what the viewer last told it to, not what scrolling implies. */
  function userSetPlaying(next) {
    userPaused = !next;
    setPlaying(next);
  }

  playBtn.addEventListener("click", () => userSetPlaying(!transport.playing));
  scrub.addEventListener("input", () => {
    userPaused = true;
    seek((Number(scrub.value) / 1000) * TOTAL, { pause: true });
  });

  document.addEventListener("keydown", (event) => {
    if (event.target && /^(INPUT|TEXTAREA|SELECT)$/.test(event.target.tagName)) return;
    // Scoped to the stage being on screen. These shortcuts used to own the
    // whole document, which was fine when the walkthrough *was* the page --
    // on a twelve-section landing page it means Space stops scrolling.
    if (!stageVisible) return;
    if (event.code === "Space") { event.preventDefault(); userSetPlaying(!transport.playing); }
    else if (event.key === "ArrowRight") { userPaused = true; seek(transport.t + 2, { pause: true }); }
    else if (event.key === "ArrowLeft") { userPaused = true; seek(transport.t - 2, { pause: true }); }
  });

  // Playing an animation nobody is looking at wastes a demo laptop's battery
  // and, worse, its frame budget. The film is now several screens down, so
  // this is also what stops it from having already looped twice -- or from
  // still sitting on the opening black frame -- by the time anyone arrives.
  if (window.IntersectionObserver) {
    new IntersectionObserver((entries) => {
      stageVisible = entries[0].isIntersecting;
      if (!stageVisible) {
        if (transport.playing) setPlaying(false);
      } else if (!transport.playing && !userPaused && !reduced) {
        setPlaying(true);
      }
    }, { threshold: 0.15 }).observe(stage);
  }

  // A background tab throttles rAF rather than stopping it, and the 24Hz
  // guard below is a plain interval that keeps running regardless.
  document.addEventListener("visibilitychange", () => {
    if (document.hidden && transport.playing) setPlaying(false);
    else if (!document.hidden && stageVisible && !userPaused && !reduced) setPlaying(true);
  });
  window.addEventListener("resize", () => applyCamera(transport.t));

  /* ── topology explorer (the section below the hero) ──────────────────── */

  let orbit = null;

  function selectNode(node) {
    const paths = blastPaths(ctx.graph, node);
    const prop = propagate(ctx.graph, [node]);
    $("topo-selected").textContent = node;
    $("topo-summary").textContent = paths.length
      ? prop.total - 1 + " affected · " + prop.entry.filter((n) => n !== node).length + " user-facing"
      : "nothing depends on it";

    const list = $("topo-paths");
    list.replaceChildren(...(paths.length ? paths.map((chain) => {
      const item = el("li");
      item.dataset.kind = chain.length === 2 ? "direct"
        : prop.entry.indexOf(chain[0]) >= 0 ? "entry" : "transitive";
      item.append(el("span", "", chain.join(" → ")));
      const hops = el("em", "", "  " + (chain.length - 1) + (chain.length === 2 ? " hop" : " hops"));
      item.append(hops);
      return item;
    }) : [el("li", "", "Nothing depends on " + node + " — a change here has no blast radius.")]));

    if (orbit) {
      const roles = {};
      ctx.graph.nodes.forEach((n) => { roles[n] = "idle"; });
      roles[node] = "changed";
      prop.direct.forEach((n) => { if (n !== node) roles[n] = "direct"; });
      prop.transitive.forEach((n) => { roles[n] = "transitive"; });
      paths.forEach((chain) => { if (chain.length === 2) roles[chain[0]] = "direct"; });
      orbit.setRoles(roles);
      orbit.select(node);
    }
  }

  function mountOrbit() {
    const viewport = $("topo-viewport");
    const canvas = $("topo-canvas");
    orbit = window.AegisTopology3D && window.AegisTopology3D.mount(canvas, { onSelect: selectNode });
    if (!orbit) { viewport.dataset.fallback = "true"; return; }
    orbit.setTopology(ctx.graph.nodes, ctx.graph.dependsOn, TARGET);
    orbit.resize();
  }

  /* ── data layer ─────────────────────────────────────────────────────── */

  async function getJSON(path) {
    try {
      const response = await fetch(path, { headers: { Accept: "application/json" } });
      if (!response.ok) return null;
      return await response.json();
    } catch (error) {
      return null;            // no backend: the shipped fixture stands in
    }
  }

  async function hydrate() {
    const [topology, telemetry, health, metrics] = await Promise.all([
      getJSON("/api/topology"), getJSON("/api/telemetry"), getJSON("/api/health"), getJSON("/api/metrics"),
    ]);

    if (topology && Array.isArray(topology.nodes) && topology.nodes.length) {
      ctx.graph = indexTopology(topology);
      ctx.live = true;
    }
    if (telemetry && Array.isArray(telemetry.metrics) && telemetry.metrics.length) {
      ctx.metrics = telemetry.metrics;
      ctx.live = true;
    }
    if (health && health.extraction_provider) {
      film.provider.textContent = health.extraction_provider;
    }
    // /api/metrics returns {stages: {<stage>: {count, p50_ms, p95_ms, ...}}},
    // keyed by the stage names in backend/common/metrics.py. Reading a
    // differently-shaped payload here would leave the strip showing its
    // shipped numbers forever while the copy above claims they came from the
    // running process — a sentence the page would be quietly making untrue.
    let readLive = false;
    if (metrics && metrics.stages) {
      const stat = (stage, id) => {
        const entry = metrics.stages[stage];
        if (!entry || typeof entry.p50_ms !== "number" || !entry.count) return;
        $(id).textContent = entry.p50_ms.toFixed(2) + " ms";
        readLive = true;
      };
      stat("turn_total", "perf-turn");
      stat("risk_evaluation", "perf-risk");
      stat("working_set_query", "perf-read");
    }

    $("perf-nodes").textContent = ctx.graph.nodes.length;
    // A server that has not processed a turn yet reports no stages at all, so
    // "live figures" would be a claim about numbers nobody measured. The
    // shipped ones stand in and the strip says where they came from.
    const perfNote = $("perf-source");
    if (perfNote) {
      perfNote.textContent = readLive
        ? "measured by this process"
        : "shipped benchmark — this process has not run a turn yet";
      perfNote.dataset.live = String(readLive);
    }
    $("source-note").textContent = ctx.live ? "rehearsed demo · live topology" : "rehearsed demo";

    recompute();
    layoutBlast();
    if (orbit) {
      orbit.setTopology(ctx.graph.nodes, ctx.graph.dependsOn, TARGET);
    }
    selectNode(TARGET);
    render(transport.t);
  }

  /* ── boot ───────────────────────────────────────────────────────────── */

  layoutBlast();
  mountOrbit();
  selectNode(TARGET);
  render(0);
  startClock();
  if (reduced) {
    // No autoplay, and the first frame is a readable one rather than black.
    seek(CUE.grounding + 2.4, { pause: true });
  } else if (!window.IntersectionObserver) {
    // No visibility signal available: fall back to the old behaviour and
    // play, rather than shipping a stage that never starts.
    setPlaying(true);
  } else {
    // The observer above starts it the moment the stage is on screen.
    setPlaying(false);
  }
  hydrate();
})();
