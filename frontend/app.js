/* AEGIS operator console.
 *
 * Vanilla on purpose. The page is served by the Python backend and has to
 * run on a demo laptop with no toolchain three days before a deadline; a
 * bundler would add a build step and a node_modules tree without changing
 * anything a judge can see.
 *
 * Two data paths:
 *   - an SSE stream for live events (fast, incremental, what makes the room
 *     feel alive);
 *   - a REST snapshot for authoritative state (correct, ordered, and the
 *     thing to fall back on whenever the stream reconnects).
 *
 * The stream is treated as a *hint that something changed*, never as the
 * source of truth. Rebuilding derived state from a partial event feed is how
 * a UI ends up quietly disagreeing with the system it is displaying.
 */

(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);

  const els = {
    conversation: $("conversation"),
    conversationEmpty: $("conversation-empty"),
    turnCount: $("turn-count"),
    interventions: $("interventions"),
    interventionsCount: $("interventions-count"),
    metrics: $("metrics"),
    evidenceCount: $("evidence-count"),
    connection: $("connection"),
    listening: $("listening-chip"),
    windowChip: $("window-chip"),
    windowValue: $("window-value"),
    providerValue: $("provider-value"),
    refresh: $("refresh-btn"),
    textForm: $("text-form"),
    textInput: $("text-input"),
    evidenceForm: $("evidence-form"),
    evidenceMetric: $("evidence-metric"),
    evidenceValue: $("evidence-value"),
    evidenceUncertain: $("evidence-uncertain"),
  };

  const groups = {
    hypotheses: { list: $("hypotheses"), count: $("hypotheses-count"), empty: $("hypotheses-empty") },
    facts:      { list: $("facts"),      count: $("facts-count"),      empty: $("facts-empty") },
    decisions:  { list: $("decisions"),  count: $("decisions-count"),  empty: $("decisions-empty") },
    actions:    { list: $("actions"),    count: $("actions-count"),    empty: $("actions-empty") },
  };

  const state = {
    turns: new Map(),        // turn_id -> { element, claims: [] }
    seenInterventions: new Set(),
    contestedMetrics: new Set(),
    token: new URLSearchParams(location.search).get("token") || "",
    refreshTimer: null,
  };

  /* ── helpers ─────────────────────────────────────────────────────── */

  const authHeaders = () => {
    const headers = { "Content-Type": "application/json" };
    if (state.token) headers["Authorization"] = `Bearer ${state.token}`;
    return headers;
  };

  const el = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text; // textContent, never innerHTML
    return node;
  };

  const speakerLabel = (uid) => (uid === "text-client" ? "typed" : `uid ${uid}`);

  const clock = (iso) => {
    if (!iso) return "";
    const at = new Date(iso);
    return Number.isNaN(at.getTime())
      ? ""
      : at.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  };

  const flash = (node, kind) => {
    node.animate(
      [
        { boxShadow: `0 0 0 0 ${kind === "high" ? "rgba(255,92,92,.55)" : "rgba(91,140,255,.45)"}` },
        { boxShadow: "0 0 0 12px rgba(0,0,0,0)" },
      ],
      { duration: 700, easing: "ease-out" }
    );
  };

  const scrollToEnd = (container) => {
    // Only auto-scroll when the reader is already at the bottom: yanking the
    // view while someone is reading back an earlier turn is hostile.
    const nearBottom = container.scrollHeight - container.scrollTop - container.clientHeight < 120;
    if (nearBottom) container.scrollTop = container.scrollHeight;
  };

  /* ── conversation ────────────────────────────────────────────────── */

  function renderTurn(payload) {
    if (state.turns.has(payload.turn_id)) return;

    if (els.conversationEmpty) els.conversationEmpty.remove();

    const turn = el("article", "turn");
    const head = el("div", "turn-head");
    head.append(el("span", "speaker", speakerLabel(payload.uid)));
    head.append(el("span", "", clock(payload.at)));
    if (payload.modality && payload.modality !== "voice") {
      head.append(el("span", "badge-modality", payload.modality));
    }

    const claims = el("div", "claims");

    turn.append(head, el("p", "turn-text", payload.text), claims);
    els.conversation.append(turn);
    state.turns.set(payload.turn_id, { element: turn, claims });
    scrollToEnd(els.conversation);

    els.turnCount.textContent = `${state.turns.size} ${state.turns.size === 1 ? "turn" : "turns"}`;
    pulseListening("active");
  }

  // The chip that shows what AEGIS understood an utterance to be. Shared by
  // the live stream and by hydration so a reloaded page shows the same thing
  // a watched one does.
  function claimTag(payload) {
    const tag = el("span", "claim-tag", payload.type.replace(/_/g, " "));
    tag.dataset.type = payload.type;
    if (payload.metric_ref) tag.title = `metric: ${payload.metric_ref}`;
    else if (payload.target_ref) tag.title = `target: ${payload.target_ref}`;
    return tag;
  }

  function attachClaim(turnId, payload) {
    // Keyed by turn where the caller knows it. The live stream does not carry
    // the turn id on a claim event, so it falls back to the newest turn --
    // correct there, because a claim is published while its own turn is still
    // the most recent one.
    const turn = turnId ? state.turns.get(turnId) : [...state.turns.values()].pop();
    if (!turn) return;
    turn.claims.append(claimTag(payload));
  }

  function renderClaim(payload) {
    attachClaim(payload.turn_id || payload.source_turn_id, payload);
  }

  /* ── interventions ───────────────────────────────────────────────── */

  function renderIntervention(payload) {
    const key = `${payload.subject_claim_id || ""}:${payload.text || payload.outcome}`;
    if (state.seenInterventions.has(key)) return;
    state.seenInterventions.add(key);

    const emptyBlock = els.interventions.querySelector(".empty");
    if (emptyBlock) emptyBlock.remove();

    const card = el("article", "intervention");
    card.dataset.tier = payload.risk_tier || "";
    card.dataset.spoken = String(Boolean(payload.spoken));

    const head = el("div", "intervention-head");
    head.append(el("span", "tier", payload.risk_tier || "STATUS"));
    head.append(el("span", "outcome", describeOutcome(payload)));
    card.append(head);

    if (payload.text) card.append(el("p", "spoken", payload.text));

    if (Array.isArray(payload.reasons) && payload.reasons.length) {
      const list = el("ul", "reasons");
      payload.reasons.forEach((reason) => list.append(el("li", "", reason)));
      card.append(list);
    }

    els.interventions.append(card);
    scrollToEnd(els.interventions);
    els.interventionsCount.textContent = state.seenInterventions.size;

    if (payload.spoken) {
      flash(card, payload.risk_tier === "HIGH" ? "high" : "normal");
      pulseListening("speaking");
      setTimeout(() => pulseListening("active"), 2600);
    }
  }

  function describeOutcome(payload) {
    if (payload.intervention_kind === "status") return "status summary, on request";
    switch (payload.outcome) {
      case "spoken": return "spoken over the call";
      case "queued_rate_limited": return "held back — rate limit";
      case "suppressed_already_said": return "already said, not repeated";
      case "suppressed_low_risk": return "no action needed";
      case "delivery_failed": return "delivery failed";
      default: return payload.outcome || "";
    }
  }

  /* ── state panel ─────────────────────────────────────────────────── */

  function renderState(view) {
    state.contestedMetrics = new Set(
      (view.hypotheses || [])
        .filter((h) => h.status === "stale" && h.metric_ref)
        .map((h) => h.metric_ref)
    );

    fillGroup(groups.hypotheses, view.hypotheses, (item) => {
      const node = el("li", "item");
      node.dataset.stale = String(item.status === "stale");
      node.append(el("p", "item-text", item.text));
      const meta = el("div", "item-meta");
      meta.append(pill(item.status === "stale" ? "stale" : "active",
                       item.status === "stale" ? "pill-stale" : "pill-active"));
      if (item.metric_ref) meta.append(el("span", "", item.metric_ref));
      if (item.reinforcement_count > 0) {
        meta.append(el("span", "", `corroborated ×${item.reinforcement_count}`));
      }
      meta.append(el("span", "", speakerLabel(item.speaker_uid)));
      node.append(meta);
      return node;
    });

    fillGroup(groups.facts, view.facts, (item) => {
      const node = el("li", "item");
      node.append(el("p", "item-text", item.text));
      const meta = el("div", "item-meta");
      meta.append(el("span", "", speakerLabel(item.speaker_uid)));
      meta.append(el("span", "", clock(item.timestamp)));
      node.append(meta);
      return node;
    });

    fillGroup(groups.decisions, view.decisions, (item) => {
      const node = el("li", "item");
      node.append(el("p", "item-text", item.text));
      const meta = el("div", "item-meta");
      if (item.stance) meta.append(pill(item.stance, "pill-" + item.stance));
      if (item.target_ref) meta.append(el("span", "", item.target_ref));
      meta.append(el("span", "", speakerLabel(item.speaker_uid)));
      node.append(meta);
      return node;
    });

    fillGroup(groups.actions, view.proposed_actions, (item) => {
      const node = el("li", "item");
      node.append(el("p", "item-text", item.text));
      const meta = el("div", "item-meta");
      meta.append(pill(item.status, "pill-" + item.status));
      if (item.risk_verdict) {
        meta.append(pill(item.risk_verdict.risk_tier,
                         "pill-" + item.risk_verdict.risk_tier.toLowerCase()));
      }
      meta.append(el("span", "", `${item.action_kind} ${item.target_ref}`));
      if (item.resolved_by_uid) {
        // Who authorised it is the audit trail the product is built around.
        meta.append(el("span", "", `by ${speakerLabel(item.resolved_by_uid)}`));
      }
      node.append(meta);
      return node;
    });

    els.evidenceCount.textContent = (view.evidence || []).length;

    // Hydrate interventions
    if (view.interventions && view.interventions.length > 0) {
      view.interventions.forEach((item) => {
        renderIntervention({
          subject_claim_id: item.subject_claim_id,
          text: item.spoken_text,
          outcome: item.outcome,
          risk_tier: item.risk_tier,
          spoken: !!item.spoken_text,
          reasons: item.reasons
        });
      });
    }

    // Hydrate conversation.
    //
    // The claim *type* is carried through as well as the text. Without it the
    // rebuilt turns render an empty `.claims` row, and the chips that show
    // AEGIS classifying speech -- fact, hypothesis, proposed action -- are
    // gone for good after a reload. Those chips are the most legible evidence
    // in the console that anything is being reasoned about at all, so losing
    // them on refresh makes a working system look inert.
    //
    // The collection a claim came back in is what names its type: the state
    // view groups them rather than tagging them individually.
    const allClaims = [
      ...(view.facts || []).map((c) => ({ ...c, type: "fact" })),
      ...(view.hypotheses || []).map((c) => ({ ...c, type: "hypothesis" })),
      ...(view.decisions || []).map((c) => ({ ...c, type: "decision" })),
      ...(view.proposed_actions || []).map((c) => ({ ...c, type: "proposed_action" })),
    ];
    const turnsById = new Map();
    for (const claim of allClaims) {
      if (!claim.source_turn_id) continue;
      if (!turnsById.has(claim.source_turn_id)) {
        turnsById.set(claim.source_turn_id, {
          turn_id: claim.source_turn_id,
          uid: claim.speaker_uid,
          at: claim.timestamp,
          modality: claim.source_modality || "voice",
          texts: [],
          claims: []
        });
      }
      const turn = turnsById.get(claim.source_turn_id);
      if (!turn.texts.includes(claim.text)) turn.texts.push(claim.text);
      turn.claims.push(claim);
    }
    const sortedTurns = Array.from(turnsById.values()).sort((a, b) => new Date(a.at) - new Date(b.at));
    for (const turnData of sortedTurns) {
      renderTurn({
        turn_id: turnData.turn_id,
        uid: turnData.uid,
        at: turnData.at,
        modality: turnData.modality,
        text: turnData.texts.join(" ")
      });
      const entry = state.turns.get(turnData.turn_id);
      if (!entry) continue;
      // Rebuilt from scratch, not appended to. refresh() runs after every
      // pipeline event, and renderTurn skips a turn it has already drawn --
      // so appending here puts a second copy of every chip on the row on the
      // next event, and a third on the one after that. Clearing first also
      // keeps the authoritative /api/state ahead of anything the event
      // stream added, which is the rule the rest of this file follows.
      entry.claims.replaceChildren(...turnData.claims.map(claimTag));
    }
  }

  function pill(text, className) {
    return el("span", `pill ${className || ""}`.trim(), text);
  }

  function fillGroup(group, items, render) {
    const list = items || [];
    group.list.replaceChildren(...list.map(render));
    group.count.textContent = list.length;
    group.empty.hidden = list.length > 0;
  }

  function renderMetrics(metrics) {
    els.metrics.replaceChildren(
      ...metrics.map((metric) => {
        const node = el("li", "metric");
        node.dataset.contested = String(state.contestedMetrics.has(metric.name));
        node.append(el("span", "metric-name", metric.name));
        const value = metric.unit === "%" ? `${metric.current_value}%`
                    : metric.unit ? `${metric.current_value} ${metric.unit}`
                    : String(metric.current_value);
        node.append(el("span", "metric-value", value));
        node.title = metric.description || "";
        return node;
      })
    );

    if (els.evidenceMetric && !els.evidenceMetric.options.length) {
      metrics.forEach((metric) => {
        const option = document.createElement("option");
        option.value = metric.name;
        option.textContent = metric.name;
        els.evidenceMetric.append(option);
      });
    }
  }

  function pulseListening(mode) {
    els.listening.dataset.state = mode;
    els.listening.querySelector(".status-label").textContent =
      mode === "speaking" ? "Intervening" : "Listening";
  }

  /* ── data ────────────────────────────────────────────────────────── */

  async function refresh() {
    try {
      const [stateResponse, telemetryResponse, healthResponse] = await Promise.all([
        fetch("/api/state"),
        fetch("/api/telemetry"),
        fetch("/api/health"),
      ]);
      if (stateResponse.ok) renderState(await stateResponse.json());
      if (telemetryResponse.ok) renderMetrics((await telemetryResponse.json()).metrics || []);
      if (healthResponse.ok) {
        const health = await healthResponse.json();
        els.providerValue.textContent = health.extraction_provider || "—";
        els.windowValue.textContent = health.window_open ? "open" : "cooling";
        els.windowChip.dataset.state = health.window_open ? "open" : "closed";
      }
    } catch (error) {
      console.warn("state refresh failed", error);
    }
  }

  function scheduleRefresh() {
    // Coalesce bursts: a single turn can emit several events, and refetching
    // per event would hammer the backend for one visible change.
    clearTimeout(state.refreshTimer);
    state.refreshTimer = setTimeout(refresh, 160);
  }

  function connect() {
    els.connection.dataset.state = "connecting";
    els.connection.textContent = "connecting…";

    const source = new EventSource("/api/events");

    source.addEventListener("open", () => {
      els.connection.dataset.state = "live";
      els.connection.textContent = "live";
      pulseListening("active");
      refresh();
    });

    const on = (kind, handler) =>
      source.addEventListener(kind, (event) => {
        let payload;
        try {
          payload = JSON.parse(event.data);
        } catch {
          return;
        }
        handler(payload);
      });

    on("transcript", renderTurn);
    on("claim", renderClaim);
    on("intervention", (payload) => { renderIntervention(payload); scheduleRefresh(); });
    on("resolution", scheduleRefresh);
    on("evidence", scheduleRefresh);
    on("state_changed", scheduleRefresh);
    on("risk_verdict", scheduleRefresh);

    source.addEventListener("error", () => {
      // EventSource reconnects on its own; the refresh on reopen is what
      // repairs anything missed while the connection was down.
      els.connection.dataset.state = "lost";
      els.connection.textContent = "reconnecting…";
    });
  }

  /* ── input ───────────────────────────────────────────────────────── */

  els.textForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const text = els.textInput.value.trim();
    if (!text) return;
    els.textInput.value = "";
    try {
      const response = await fetch("/api/text", {
        method: "POST",
        headers: authHeaders(),
        body: JSON.stringify({ text, uid: "text-client" }),
      });
      if (!response.ok) throw new Error((await response.json()).error?.message || "failed");
    } catch (error) {
      els.textInput.value = text; // never silently swallow what someone typed
      console.error("text ingest failed", error);
      alert(`Could not send: ${error.message}`);
    }
  });

  els.evidenceForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const raw = els.evidenceValue.value.trim();
    if (!raw) return;
    const numeric = Number(raw);
    try {
      const response = await fetch("/api/evidence", {
        method: "POST",
        headers: authHeaders(),
        body: JSON.stringify({
          metric_name: els.evidenceMetric.value,
          value: Number.isNaN(numeric) ? raw : numeric,
          unit: "%",
          uploader_uid: "text-client",
          extraction_certainty: els.evidenceUncertain.checked ? "low" : "high",
        }),
      });
      if (!response.ok) throw new Error((await response.json()).error?.message || "failed");
      els.evidenceValue.value = "";
      scheduleRefresh();
    } catch (error) {
      console.error("evidence submit failed", error);
      alert(`Could not submit: ${error.message}`);
    }
  });

  els.refresh.addEventListener("click", refresh);

  refresh();
  connect();
})();
