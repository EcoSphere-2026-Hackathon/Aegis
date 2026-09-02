/* AEGIS landing hero — continuous composition.
 * The console is the real one (backend/frontend styles.css values, verbatim).
 * Every sentence AEGIS speaks is composed the way governor/speech.py composes it;
 * the blast radius is computed here by the same reverse BFS as risk_engine/topology.py.
 */
const { useComposition, CompositionStage, Shot, Captions, Easing, animate } = window;
const { useTweaks, TweaksPanel, TweakSection, TweakToggle } = window;

/* ── palette: frontend/styles.css :root ───────────────────────────────── */
const C = {
  bg: '#0a0c10', raised: '#11141b', panel: '#12161e', inset: '#0d1016',
  border: '#1e2430', bstrong: '#2a3242',
  text: '#e6e9ef', muted: '#97a0b0', faint: '#6b7484',
  accent: '#5b8cff', accentDim: '#2c4a8f',
  high: '#ff5c5c', highBg: '#2a1216', medium: '#ffb454', mediumBg: '#2a1f0e',
  ok: '#3ddc97', okBg: '#0e2620', stale: '#8b93a4',
};
const MONO = '"IBM Plex Mono", ui-monospace, "SF Mono", Menlo, Consolas, monospace';
const SANS = '-apple-system, BlinkMacSystemFont, "Segoe UI", Inter, Roboto, "Helvetica Neue", Arial, sans-serif';
const SHADOW = '0 1px 2px rgba(0,0,0,.4), 0 8px 24px rgba(0,0,0,.28)';

/* ── three motion helpers, nothing else eases ─────────────────────────── */
const MOTION = {
  enter: (start, d) => animate({ from: 0, to: 1, start, end: start + (d || 0.5), ease: Easing.easeOutCubic }),
  draw: (start, end) => animate({ from: 0, to: 1, start, end, ease: Easing.easeInOutCubic }),
  pop: (start, d) => animate({ from: 0, to: 1, start, end: start + (d || 0.42), ease: Easing.easeOutBack }),
};

/* ── topology: backend/risk_engine/topology.py build_incident_topology ─── */
const EDGES = [
  ['payment-api', 'core-db'], ['auth-service', 'core-db'], ['cache-layer', 'core-db'],
  ['analytics-pipeline', 'core-db'], ['billing-service', 'payment-api'],
  ['notification-service', 'billing-service'], ['api-gateway', 'auth-service'],
  ['api-gateway', 'payment-api'], ['search-index', 'analytics-pipeline'],
  ['user-service', 'auth-service'],
];
const REV = {};
EDGES.forEach(function (e) { (REV[e[1]] = REV[e[1]] || []).push(e[0]); });

function propagate(broken) {           // multi-source reverse BFS over depends_on
  const seen = new Set(broken), q = broken.slice(), trans = [], depth = {};
  broken.forEach(function (n) { depth[n] = 1; });
  while (q.length) {
    const cur = q.shift();
    (REV[cur] || []).forEach(function (d) {
      if (seen.has(d)) return;
      seen.add(d); depth[d] = depth[cur] + 1; trans.push(d); q.push(d);
    });
  }
  const all = Array.from(seen);
  return {
    direct: broken.slice().sort(), transitive: trans.slice().sort(),
    entry: all.filter(function (n) { return !(REV[n] || []).length; }).sort(),
    total: all.length, depth: depth,
  };
}
const BLAST = propagate(['payment-api', 'auth-service']);

const NODE_XY = {
  'core-db': [96, 300],
  'payment-api': [392, 122], 'auth-service': [392, 262],
  'cache-layer': [392, 402], 'analytics-pipeline': [392, 522],
  'billing-service': [704, 74], 'api-gateway': [704, 200],
  'user-service': [704, 330], 'search-index': [704, 522],
  'notification-service': [1004, 74],
};

/* ── speech: backend/governor/speech.py composition ───────────────────── */
const bytes = function (s) { return new TextEncoder().encode(s).length; };
const SPEAK_MAX = 512;

const SPEAK_1 = 'Hold — telemetry shows pool utilization at 91%, not 40%. Want to re-check before ruling it out?';

const F_BLAST = 'Rollback of core-db to v2.3 will break ' + BLAST.direct.join(' and ')
  + " — they're on schema v17, incompatible with v2.3, cascading to " + BLAST.transitive.length
  + ' more services including user-facing ' + BLAST.entry.slice(0, 2).join(' and ');
const F_STALE = "The pool root cause still isn't confirmed — it was contradicted and never re-established.";
const OPENER = 'Hold — two issues.';
const CLOSER = 'Do you want to go ahead anyway?';
const SPEAK_2 = OPENER + ' ' + F_BLAST + '. ' + F_STALE + ' ' + CLOSER;
const SPEAK_3 = 'Status. 1 theory still open and unconfirmed: it is the pool then; decisions on record: '
  + "don't rollback, let's check the pool metrics properly first; nothing awaiting a decision.";

const METRICS = [
  { name: 'pool_utilization', value: '91', unit: '%', ref: 'core-db' },
  { name: 'error_rate', value: '12.4', unit: '%', ref: 'payment-api' },
  { name: 'p99_latency', value: '2400', unit: 'ms', ref: 'payment-api' },
  { name: 'schema_version', value: 'v17', unit: '', ref: 'core-db' },
];

/* ── small console primitives ─────────────────────────────────────────── */
const Panel = function (p) {
  return (
    <section style={Object.assign({
      display: 'flex', flexDirection: 'column', minHeight: 0, background: C.panel,
      border: '1px solid ' + C.border, borderRadius: 10, boxShadow: SHADOW, overflow: 'hidden',
    }, p.style)}>
      <div style={{
        display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 10,
        padding: '11px 14px', borderBottom: '1px solid ' + C.border, background: C.raised, flexShrink: 0,
      }}>
        <h2 style={{ margin: 0, fontSize: 11, fontWeight: 650, letterSpacing: '.11em', textTransform: 'uppercase', color: C.muted }}>{p.title}</h2>
        <span style={{ fontSize: 11, color: C.faint, fontVariantNumeric: 'tabular-nums' }}>{p.right}</span>
      </div>
      {p.children}
    </section>
  );
};

const Pill = function (p) {
  return <span style={{
    fontSize: 9.5, letterSpacing: '.07em', textTransform: 'uppercase', padding: '1.5px 6px',
    borderRadius: 4, border: '1px solid ' + (p.color || C.bstrong), color: p.color || C.muted,
  }}>{p.children}</span>;
};

const Chip = function (p) {
  return (
    <div style={{
      display: 'inline-flex', alignItems: 'center', gap: 7, padding: '5px 11px',
      border: '1px solid ' + (p.color || C.border), borderRadius: 999, background: C.inset,
      fontSize: 11.5, color: p.color || C.muted, whiteSpace: 'nowrap',
    }}>
      {p.dot !== undefined && <span style={{
        width: 7, height: 7, borderRadius: '50%', background: p.dot,
        boxShadow: '0 0 0 ' + (p.ring || 0) + 'px ' + p.dot + '55',
      }} />}
      {p.k && <span style={{ color: C.faint }}>{p.k}</span>}
      <span style={{ color: p.color || C.text, fontVariantNumeric: 'tabular-nums' }}>{p.v}</span>
    </div>
  );
};

const Item = function (p) {
  return (
    <li style={Object.assign({
      border: '1px solid ' + C.border, borderRadius: 6, padding: '7px 10px',
      background: C.inset, fontSize: 12.5, listStyle: 'none',
    }, p.style)}>
      <div style={{
        color: p.stale ? C.stale : C.text,
        textDecoration: p.stale ? 'line-through' : 'none', textDecorationThickness: 1,
      }}>{p.text}</div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 7, marginTop: 5, fontSize: 10.5, color: C.faint, flexWrap: 'wrap' }}>
        {p.meta}
      </div>
    </li>
  );
};

/* ── the console, filmed ──────────────────────────────────────────────── */
function Console(props) {
  const T = props.T, K = props.K;
  const fade = function (at, d) {
    const v = MOTION.enter(at, d || 0.45)(T);
    return { opacity: v, transform: 'translateY(' + ((1 - v) * 6).toFixed(2) + 'px)' };
  };
  const on = function (at) { return T >= at; };

  const turns = [
    { at: K.FirstTurn + 0.3, who: 'Engineer A', uid: '1001', text: 'Payments are throwing 500s, seeing timeouts.', claims: [['fact', C.muted, C.bstrong]] },
    { at: K.Claim + 0.4, who: 'Engineer B', uid: '1002', text: 'Pool utilization looks fine, like 40%.', claims: [['hypothesis', C.accent, C.accentDim]] },
    { at: K.Rollback + 0.3, who: 'Engineer A', uid: '1001', text: "Okay, fine, it is the pool then. Let's rollback Core to the last version.", claims: [['hypothesis', C.accent, C.accentDim], ['proposed_action', C.medium, C.medium]] },
    { at: K.Human + 0.4, who: 'Engineer B', uid: '1002', text: "Hold — don't rollback, let's check the pool metrics properly first.", claims: [['hold', C.text, C.bstrong]] },
    { at: K.Status + 0.3, who: 'Engineer A', uid: '1001', text: 'AEGIS, status?', claims: [] },
  ].filter(function (t) { return on(t.at - 0.5); });

  const contested = on(K.Grounding + 0.9);
  const speaking = (T > K.Intervene1 + 0.5 && T < K.Intervene1 + 4.2)
    || (T > K.Intervene2 + 0.5 && T < K.Intervene2 + 5.2)
    || (T > K.Status + 2.4 && T < K.Status + 5.2);
  const listening = T > K.FirstTurn;

  /* rate-limit window: one intervention / 45s, backend/governor/governor.py */
  let win = 'open', winColor = null;
  if (T > K.Intervene1 + 0.8 && T < K.Rollback - 0.4) {
    const left = Math.max(0, Math.round(45 * (1 - (T - (K.Intervene1 + 0.8)) / ((K.Rollback - 0.4) - (K.Intervene1 + 0.8)))));
    win = 'closed · ' + left + 's'; winColor = C.medium;
  } else if (T > K.Intervene2 + 0.8 && T < K.Status + 2.2) {
    const left2 = Math.max(0, Math.round(45 * (1 - (T - (K.Intervene2 + 0.8)) / ((K.Status + 2.2) - (K.Intervene2 + 0.8)))));
    win = 'closed · ' + left2 + 's'; winColor = C.medium;
  }

  const interventions = [
    on(K.FirstTurn + 1.9) && {
      at: K.FirstTurn + 1.9, tier: null, spoken: false,
      head: 'silent', outcome: 'no risk findings',
      body: 'Ordinary incident chatter. Nothing to say.', reasons: [],
    },
    on(K.Intervene1 + 0.6) && {
      at: K.Intervene1 + 0.6, tier: 'HIGH', spoken: true, head: 'HIGH', outcome: 'spoken · warn',
      body: SPEAK_1, reasons: ['evidence_contradiction — telemetry shows pool utilization at 91%, not 40%'],
    },
    on(K.Intervene2 + 0.6) && {
      at: K.Intervene2 + 0.6, tier: 'HIGH', spoken: true, head: 'HIGH', outcome: 'spoken · warn',
      body: SPEAK_2,
      reasons: ['blast_radius_schema_break — payment-api and auth-service read core-db schema v17',
        'stale_justification — the pool root cause was contradicted and never re-established'],
    },
    on(K.Status + 2.5) && {
      at: K.Status + 2.5, tier: null, spoken: true, head: 'status', outcome: 'spoken · on request',
      body: SPEAK_3, reasons: [],
    },
  ].filter(Boolean);

  const hyp = [
    on(K.Claim + 1.1) && { at: K.Claim + 1.1, text: 'Pool utilization is fine, ~40% — core-db', stale: on(K.Grounding + 1.4), by: '1002' },
    on(K.Rollback + 1.2) && { at: K.Rollback + 1.2, text: 'It is the pool — core-db', stale: on(K.Stale + 1.1), by: '1001' },
  ].filter(Boolean);

  return (
    <div style={{ width: 1440, height: 810, background: C.bg, color: C.text, fontFamily: SANS, fontSize: 14, lineHeight: 1.5, display: 'flex', flexDirection: 'column' }}>
      {/* topbar */}
      <header style={{
        display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 16,
        padding: '14px 20px', borderBottom: '1px solid ' + C.border,
        background: 'linear-gradient(180deg,' + C.raised + ',' + C.bg + ')', flexShrink: 0,
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <span style={{
            width: 30, height: 30, borderRadius: 8,
            background: 'radial-gradient(circle at 32% 28%, rgba(255,255,255,.5), transparent 55%), linear-gradient(145deg,' + C.accent + ',#7d5bff)',
            boxShadow: 'inset 0 0 0 1px rgba(255,255,255,.09), 0 4px 14px rgba(91,140,255,.32)',
          }} />
          <div>
            <h1 style={{ margin: 0, fontSize: 15, fontWeight: 650, letterSpacing: '.14em' }}>AEGIS</h1>
            <p style={{ margin: 0, fontSize: 11, color: C.faint, letterSpacing: '.05em' }}>Incident Commander</p>
          </div>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <Chip dot={speaking ? C.high : C.ok} ring={speaking ? 7 : (listening ? 4 : 0)}
            color={speaking ? C.high : null} v={speaking ? 'Speaking' : (listening ? 'Listening' : 'Idle')} />
          <Chip k="Window" v={win} color={winColor} />
          <Chip k="Extractor" v="deterministic" />
        </div>
      </header>

      {/* grid */}
      <main style={{
        flex: 1, minHeight: 0, display: 'grid', gap: 16, padding: 16,
        gridTemplateColumns: 'minmax(0,1.25fr) minmax(0,1fr) minmax(0,1fr)',
        gridTemplateRows: 'minmax(0,1fr) minmax(0,0.85fr)',
        gridTemplateAreas: '"conversation state interventions" "conversation state evidence"',
      }}>
        {/* conversation */}
        <Panel title="Conversation" right={turns.length + ' turns'} style={{ gridArea: 'conversation' }}>
          <div style={{ flex: 1, minHeight: 0, overflow: 'hidden', padding: '12px 14px', display: 'flex', flexDirection: 'column', gap: 10 }}>
            {!turns.length && (
              <div style={{ margin: 'auto', textAlign: 'center', maxWidth: '30ch', padding: '24px 0' }}>
                <p style={{ margin: '0 0 5px', fontSize: 13, fontWeight: 600, color: C.muted }}>Nothing said yet</p>
                <p style={{ margin: 0, fontSize: 12, color: C.faint }}>Transcripts appear here as people speak. AEGIS stays silent unless something needs saying.</p>
              </div>
            )}
            {turns.map(function (t, i) {
              return (
                <div key={i} style={Object.assign({ display: 'flex', flexDirection: 'column', gap: 5 }, fade(t.at))}>
                  <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, fontSize: 11, color: C.faint }}>
                    <span style={{ fontWeight: 640, color: C.muted, fontFamily: MONO, fontSize: 11 }}>{t.who}</span>
                    <span style={{ fontSize: 9.5, letterSpacing: '.07em', textTransform: 'uppercase', padding: '1px 5px', borderRadius: 4, border: '1px solid ' + C.bstrong, color: C.faint }}>voice</span>
                    <span style={{ fontFamily: MONO }}>uid {t.uid}</span>
                  </div>
                  <div style={{ background: C.inset, border: '1px solid ' + C.border, borderRadius: 6, padding: '8px 11px', color: C.text }}>{t.text}</div>
                  {!!t.claims.length && (
                    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 5 }}>
                      {t.claims.map(function (c, j) {
                        const v = MOTION.pop(t.at + 0.5 + j * 0.18)(T);
                        return <span key={j} style={{
                          fontSize: 10.5, fontFamily: MONO, padding: '2px 7px', borderRadius: 999,
                          border: '1px solid ' + c[2], color: c[1], background: C.inset,
                          opacity: v, transform: 'scale(' + (0.86 + 0.14 * v) + ')',
                        }}>{c[0]}</span>;
                      })}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
          <div style={{ display: 'flex', gap: 8, padding: '11px 14px', borderTop: '1px solid ' + C.border, background: C.raised }}>
            <div style={{ flex: 1, background: C.inset, border: '1px solid ' + C.bstrong, borderRadius: 6, padding: '7px 10px', fontSize: 13, color: C.faint }}>Type a note into the incident…</div>
            <div style={{ background: C.accent, borderRadius: 6, padding: '7px 14px', fontSize: 13, fontWeight: 560, color: '#fff' }}>Send</div>
          </div>
        </Panel>

        {/* incident state */}
        <Panel title="Incident state" right="" style={{ gridArea: 'state' }}>
          <div style={{ flex: 1, minHeight: 0, overflow: 'hidden', padding: '4px 0 10px' }}>
            {[
              { key: 'Theories', empty: 'No theories on record.', rows: hyp.map(function (h, i) {
                return <Item key={i} text={h.text} stale={h.stale} style={fade(h.at)}
                  meta={<>
                    <Pill color={h.stale ? C.stale : C.ok}>{h.stale ? 'stale' : 'active'}</Pill>
                    <span style={{ fontFamily: MONO }}>uid {h.by}</span>
                    {h.stale && <span style={{ color: C.high }}>contradicted by telemetry</span>}
                  </>} />;
              }) },
              { key: 'Facts', empty: 'Nothing established yet.', rows: on(K.FirstTurn + 1.2) ? [
                <Item key="f" text="Payment-api returning 500s and timeouts" style={fade(K.FirstTurn + 1.2)}
                  meta={<><Pill color={C.ok}>corroborated</Pill><span style={{ fontFamily: MONO }}>error_rate 12.4%</span></>} />,
              ] : [] },
              { key: 'Decision ledger', empty: 'Nothing decided yet.', rows: on(K.Human + 1.6) ? [
                <Item key="d" text="Don't rollback — check the pool metrics properly first" style={fade(K.Human + 1.6)}
                  meta={<><Pill color={C.muted}>hold</Pill><span style={{ fontFamily: MONO }}>uid 1002 · core-db</span></>} />,
              ] : [] },
              { key: 'Proposed actions', empty: 'Nothing proposed yet.', rows: on(K.Rollback + 1.5) ? [
                <Item key="a" text="rollback · core-db → schema v2.3" style={fade(K.Rollback + 1.5)}
                  meta={<>
                    <Pill color={on(K.Human + 1.6) ? C.muted : C.medium}>{on(K.Human + 1.6) ? 'held' : 'pending'}</Pill>
                    <Pill color={on(K.Stale + 1.6) ? C.high : (on(K.BlastRadius + 3) ? C.high : C.faint)}>{on(K.BlastRadius + 3) ? 'HIGH' : 'evaluating'}</Pill>
                    <span style={{ fontFamily: MONO }}>justified by: it is the pool</span>
                    {on(K.Stale + 1.6) && <span style={{ color: C.medium }}>re-evaluated ×1</span>}
                  </>} />,
              ] : [] },
            ].map(function (g, i) {
              return (
                <div key={i} style={{ padding: '10px 14px 4px', borderTop: i ? '1px solid ' + C.border : 'none' }}>
                  <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 7 }}>
                    <h3 style={{ margin: 0, fontSize: 10.5, fontWeight: 640, letterSpacing: '.1em', textTransform: 'uppercase', color: C.faint }}>{g.key}</h3>
                    <span style={{ fontSize: 11, color: C.faint, fontVariantNumeric: 'tabular-nums' }}>{g.rows.length}</span>
                  </div>
                  {g.rows.length
                    ? <ul style={{ margin: 0, padding: 0, display: 'flex', flexDirection: 'column', gap: 6 }}>{g.rows}</ul>
                    : <p style={{ margin: 0, fontSize: 12, color: C.faint, fontStyle: 'italic' }}>{g.empty}</p>}
                </div>
              );
            })}
          </div>
        </Panel>

        {/* interventions */}
        <Panel title="Interventions" right={String(interventions.filter(function (i) { return i.spoken; }).length)} style={{ gridArea: 'interventions' }}>
          <div style={{ flex: 1, minHeight: 0, overflow: 'hidden', padding: '12px 14px', display: 'flex', flexDirection: 'column', gap: 10 }}>
            {!interventions.length && (
              <div style={{ margin: 'auto', textAlign: 'center', maxWidth: '30ch', padding: '24px 0' }}>
                <p style={{ margin: '0 0 5px', fontSize: 13, fontWeight: 600, color: C.muted }}>Silent</p>
                <p style={{ margin: 0, fontSize: 12, color: C.faint }}>Every intervention AEGIS makes is recorded here with the exact rule it caught.</p>
              </div>
            )}
            {interventions.slice().reverse().map(function (iv, i) {
              return (
                <div key={i} style={Object.assign({
                  border: '1px solid ' + C.bstrong, borderLeft: '3px solid ' + (iv.tier === 'HIGH' ? C.high : C.stale),
                  borderRadius: 6, padding: '10px 12px',
                  background: iv.tier === 'HIGH' ? C.highBg : C.inset,
                  opacity: iv.spoken ? 1 : 0.62,
                }, fade(iv.at))}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6, fontSize: 10.5, letterSpacing: '.08em', textTransform: 'uppercase' }}>
                    <span style={{ fontWeight: 700, color: iv.tier === 'HIGH' ? C.high : C.faint }}>{iv.head}</span>
                    <span style={{ color: C.faint, letterSpacing: '.04em', textTransform: 'none', fontSize: 11 }}>{iv.outcome}</span>
                  </div>
                  <p style={{ fontSize: 14, lineHeight: 1.45, margin: '0 0 8px' }}>{iv.body}</p>
                  {!!iv.reasons.length && (
                    <ul style={{ margin: 0, paddingLeft: 16, display: 'flex', flexDirection: 'column', gap: 3 }}>
                      {iv.reasons.map(function (r, j) { return <li key={j} style={{ fontSize: 12, color: C.muted }}>{r}</li>; })}
                    </ul>
                  )}
                </div>
              );
            })}
          </div>
        </Panel>

        {/* evidence */}
        <Panel title="Evidence" right="4" style={{ gridArea: 'evidence' }}>
          <ul style={{ listStyle: 'none', margin: 0, padding: '12px 14px', display: 'flex', flexDirection: 'column', gap: 8, flex: 1, minHeight: 0 }}>
            {METRICS.map(function (m) {
              const hot = contested && m.name === 'pool_utilization';
              const glow = hot ? 0.35 + 0.35 * Math.sin(T * 3.4) : 0;
              return (
                <li key={m.name} style={{
                  display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', gap: 10,
                  padding: '8px 11px', border: '1px solid ' + (hot ? C.high : C.border), borderRadius: 6,
                  background: C.inset, boxShadow: hot ? '0 0 0 3px rgba(255,92,92,' + glow.toFixed(2) + ')' : 'none',
                }}>
                  <span style={{ fontSize: 12, color: C.muted, fontFamily: MONO }}>{m.name}</span>
                  <span style={{ fontSize: 17, fontWeight: 620, fontVariantNumeric: 'tabular-nums', color: hot ? C.high : C.text }}>
                    {m.value}<span style={{ fontSize: 12, color: C.faint }}>{m.unit}</span>
                  </span>
                </li>
              );
            })}
          </ul>
          <div style={{ borderTop: '1px solid ' + C.border, padding: '10px 14px', fontSize: 11, color: C.muted, letterSpacing: '.05em', textTransform: 'uppercase' }}>＋ Submit a reading</div>
        </Panel>
      </main>

      <footer style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '9px 20px', borderTop: '1px solid ' + C.border, background: C.raised, fontSize: 11, color: C.faint }}>
        <span style={{ color: C.ok }}>● live</span>
        <span style={{ opacity: 0.4 }}>·</span>
        <span>AEGIS never executes, blocks or overrides an action. Every consequential step needs an explicit human decision.</span>
      </footer>
    </div>
  );
}

/* ── overlay chrome ───────────────────────────────────────────────────── */
const Surface = function (p) {
  return (
    <div style={Object.assign({
      position: 'absolute', background: 'rgba(13,16,22,.93)', backdropFilter: 'blur(14px)',
      border: '1px solid ' + C.bstrong, borderRadius: 14,
      boxShadow: '0 30px 90px rgba(0,0,0,.65), inset 0 1px 0 rgba(255,255,255,.05)',
      padding: 22, color: C.text, fontFamily: SANS,
    }, p.style)}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 16 }}>
        <span style={{ width: 6, height: 6, borderRadius: 999, background: p.accent || C.accent }} />
        <span style={{ fontSize: 14, letterSpacing: '.18em', textTransform: 'uppercase', color: C.muted, fontFamily: MONO }}>{p.label}</span>
        <span style={{ flex: 1, height: 1, background: C.border }} />
        <span style={{ fontSize: 13, color: C.faint, fontFamily: MONO }}>{p.tag}</span>
      </div>
      {p.children}
    </div>
  );
};

/* grounding: claimed 40% vs measured 91% */
function GroundingSurface(props) {
  const T = props.T, s = props.start;
  const bar = MOTION.draw(s + 0.5, s + 1.9)(T);
  const hit = MOTION.enter(s + 2.1, 0.5)(T);
  const row = function (label, val, pct, color, delay) {
    const w = MOTION.draw(s + 0.5 + delay, s + 1.9 + delay)(T);
    return (
      <div style={{ marginBottom: 16 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 15, fontFamily: MONO, color: C.muted, marginBottom: 8 }}>
          <span>{label}</span><span style={{ color: color, fontSize: 24, fontWeight: 620 }}>{val}</span>
        </div>
        <div style={{ height: 14, background: C.inset, border: '1px solid ' + C.border, borderRadius: 999, overflow: 'hidden' }}>
          <div style={{ height: '100%', width: (pct * w) + '%', background: color, borderRadius: 999 }} />
        </div>
      </div>
    );
  };
  return (
    <Surface label="Evidence contradiction" tag="risk_engine · check 4" accent={C.high}
      style={{ left: 1040, top: 232, width: 760, opacity: bar > 0 ? 1 : 0 }}>
      {row('claimed · uid 1002 · voice', '40%', 40, C.medium, 0)}
      {row('measured · mock_telemetry · pool_utilization', '91%', 91, C.high, 0.35)}
      <div style={{ opacity: hit, borderTop: '1px solid ' + C.border, paddingTop: 14, fontSize: 16, color: C.muted, lineHeight: 1.5 }}>
        <span style={{ color: C.high, fontFamily: MONO, fontSize: 14 }}>EVIDENCE_CONTRADICTION · HIGH</span><br />
        The model produced a typed claim. Python compared it to the reading and set the tier.
      </div>
    </Surface>
  );
}

/* blast radius: reverse BFS over the transposed dependency graph */
function BlastSurface(props) {
  const T = props.T, s = props.start;
  const wave = function (n) {
    const d = BLAST.depth[n] || 0;
    if (!d) return 0;
    return MOTION.enter(s + 1.2 + (d - 1) * 0.85, 0.55)(T);
  };
  const nodeState = function (n) {
    if (n === 'core-db') return { c: C.accent, bg: 'rgba(91,140,255,.14)', label: 'changed' };
    if (BLAST.direct.indexOf(n) >= 0) return { c: C.high, bg: C.highBg, label: 'breaks · schema v17' };
    if (BLAST.transitive.indexOf(n) >= 0) return { c: C.medium, bg: C.mediumBg, label: BLAST.entry.indexOf(n) >= 0 ? 'user-facing' : 'cascade' };
    if (n === 'cache-layer') return { c: C.ok, bg: C.okBg, label: 'compatible v17 · v2.3' };
    return { c: C.faint, bg: C.inset, label: '' };
  };
  const counters = MOTION.enter(s + 4.4, 0.6)(T);
  return (
    <Surface label="Blast radius · rollback core-db → v2.3" tag="propagate_failure() · O(V+E)" accent={C.high}
      style={{ left: 240, top: 176, width: 1440, height: 728 }}>
      <div style={{ position: 'relative', height: 600 }}>
        <svg width="1396" height="600" style={{ position: 'absolute', inset: 0 }}>
          {EDGES.map(function (e, i) {
            const a = NODE_XY[e[0]], b = NODE_XY[e[1]];
            const lit = Math.min(wave(e[0]), 1);
            const affected = BLAST.depth[e[0]] && (BLAST.depth[e[1]] || e[1] === 'core-db');
            return (
              <g key={i}>
                <line x1={b[0]} y1={b[1]} x2={a[0]} y2={a[1]} stroke={C.border} strokeWidth="1.5" />
                {affected ? <line x1={b[0]} y1={b[1]}
                  x2={b[0] + (a[0] - b[0]) * lit} y2={b[1] + (a[1] - b[1]) * lit}
                  stroke={BLAST.direct.indexOf(e[0]) >= 0 ? C.high : C.medium} strokeWidth="2.5" /> : null}
              </g>
            );
          })}
        </svg>
        {Object.keys(NODE_XY).map(function (n) {
          const p = NODE_XY[n], st = nodeState(n);
          const w = n === 'core-db' ? MOTION.enter(s + 0.5, 0.5)(T) : wave(n);
          const active = w > 0.02;
          return (
            <div key={n} style={{
              position: 'absolute', left: p[0] - 104, top: p[1] - 28, width: 208,
              border: '1px solid ' + (active ? st.c : C.border), borderRadius: 8,
              background: active ? st.bg : C.inset, padding: '8px 10px',
              transform: 'scale(' + (0.94 + 0.06 * (active ? w : 1)) + ')',
              boxShadow: active && st.c !== C.faint ? '0 0 22px ' + st.c + '33' : 'none',
            }}>
              <div style={{ fontFamily: MONO, fontSize: 16, color: active ? C.text : C.faint }}>{n}</div>
              <div style={{ fontSize: 12, letterSpacing: '.06em', textTransform: 'uppercase', color: active ? st.c : C.faint, marginTop: 3, minHeight: 12 }}>
                {active ? st.label : 'not affected'}
              </div>
            </div>
          );
        })}
        <div style={{ position: 'absolute', right: 0, bottom: 8, display: 'flex', gap: 26, opacity: counters }}>
          {[['direct break', BLAST.direct.length, C.high], ['cascade', BLAST.transitive.length, C.medium],
            ['total affected', BLAST.total, C.text], ['user-facing', BLAST.entry.length, C.medium]].map(function (c, i) {
            return (
              <div key={i} style={{ textAlign: 'right' }}>
                <div style={{ fontSize: 46, fontWeight: 300, color: c[2], fontVariantNumeric: 'tabular-nums', lineHeight: 1 }}>{c[1]}</div>
                <div style={{ fontSize: 12, letterSpacing: '.14em', textTransform: 'uppercase', color: C.faint, marginTop: 6 }}>{c[0]}</div>
              </div>
            );
          })}
        </div>
      </div>
    </Surface>
  );
}

/* belief retraction: the justification edge breaks, the action re-enters evaluation */
function JustificationSurface(props) {
  const T = props.T, s = props.start;
  const inz = MOTION.enter(s + 0.3, 0.5)(T);
  const snap = MOTION.draw(s + 1.5, s + 2.3)(T);
  const reval = MOTION.enter(s + 2.8, 0.6)(T);
  const box = function (title, sub, color, style) {
    return (
      <div style={Object.assign({
        width: 300, border: '1px solid ' + color, borderRadius: 10, background: C.inset, padding: '14px 16px',
      }, style)}>
        <div style={{ fontSize: 12.5, letterSpacing: '.14em', textTransform: 'uppercase', color: color, marginBottom: 6 }}>{title}</div>
        <div style={{ fontSize: 17, color: C.text, lineHeight: 1.4 }}>{sub}</div>
      </div>
    );
  };
  return (
    <Surface label="Belief retraction" tag="pending_actions_justified_by · reverse lookup" accent={C.medium}
      style={{ left: 380, top: 300, width: 1160, opacity: inz }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 20 }}>
        {box('hypothesis', 'It is the pool — core-db', snap > 0.5 ? C.stale : C.accent,
          { opacity: snap > 0.5 ? 0.6 : 1, textDecoration: snap > 0.5 ? 'line-through' : 'none' })}
        <div style={{ flex: 1, position: 'relative', height: 40 }}>
          <div style={{ position: 'absolute', top: 19, left: 0, right: 0, height: 2, background: C.border }} />
          <div style={{
            position: 'absolute', top: 19, left: 0, height: 2, background: snap > 0.5 ? C.high : C.accent,
            width: (100 - snap * 46) + '%', transformOrigin: 'left',
          }} />
          <div style={{
            position: 'absolute', top: 19, right: 0, height: 2, background: snap > 0.5 ? C.high : C.accent,
            width: (snap * 0) + '%',
          }} />
          <div style={{
            position: 'absolute', top: 2, left: '50%', transform: 'translateX(-50%)',
            fontSize: 13, fontFamily: MONO, color: snap > 0.5 ? C.high : C.faint, background: 'rgba(13,16,22,.95)', padding: '0 8px',
          }}>{snap > 0.5 ? 'justification invalid' : 'justifies'}</div>
        </div>
        {box('pending action', 'rollback core-db → v2.3', reval > 0.3 ? C.high : C.medium)}
      </div>
      <div style={{ marginTop: 18, display: 'flex', gap: 12, alignItems: 'center', opacity: reval }}>
        {['re-evaluated', 'risk_engine.evaluate()', 'HIGH'].map(function (x, i) {
          return (
            <React.Fragment key={i}>
              <span style={{
                fontFamily: MONO, fontSize: 14, padding: '6px 12px', borderRadius: 6,
                border: '1px solid ' + (i === 2 ? C.high : C.bstrong), color: i === 2 ? C.high : C.muted,
              }}>{x}</span>
              {i < 2 && <span style={{ color: C.faint }}>→</span>}
            </React.Fragment>
          );
        })}
        <span style={{ marginLeft: 'auto', fontSize: 15, color: C.muted, maxWidth: 480, lineHeight: 1.45 }}>
          A verdict computed against a belief nobody holds any more only ever escalates — it cannot cascade.
        </span>
      </div>
    </Surface>
  );
}

/* 512-byte speech budget: mandatory lead + exact 0/1 knapsack */
function BudgetSurface(props) {
  const T = props.T, s = props.start;
  const inz = MOTION.enter(s + 0.2, 0.5)(T);
  const parts = [
    { label: 'opener', text: OPENER, color: C.muted, at: 0.4 },
    { label: 'finding · HIGH · blast_radius_schema_break', text: F_BLAST + '.', color: C.high, at: 0.9 },
    { label: 'finding · MEDIUM · stale_justification', text: F_STALE, color: C.medium, at: 1.7 },
    { label: 'closer', text: CLOSER, color: C.muted, at: 2.3 },
  ];
  const used = bytes(SPEAK_2);
  const meter = MOTION.draw(s + 0.6, s + 2.9)(T);
  return (
    <Surface label="Speech budget" tag="Agora speak() · 512 bytes · 0/1 knapsack" accent={C.medium}
      style={{ left: 220, top: 286, width: 1480, opacity: inz }}>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 14, marginBottom: 12 }}>
        <span style={{ fontFamily: MONO, fontSize: 40, fontWeight: 300, color: C.text, fontVariantNumeric: 'tabular-nums' }}>
          {Math.round(used * meter)}
        </span>
        <span style={{ fontFamily: MONO, fontSize: 17, color: C.faint }}>/ {SPEAK_MAX} bytes</span>
      </div>
      <div style={{ display: 'flex', height: 18, borderRadius: 999, overflow: 'hidden', background: C.inset, border: '1px solid ' + C.border, marginBottom: 20 }}>
        {parts.map(function (p, i) {
          const w = MOTION.draw(s + p.at, s + p.at + 0.5)(T);
          return <div key={i} style={{ width: (bytes(p.text + ' ') / SPEAK_MAX * 100 * w) + '%', background: p.color, opacity: 0.85 }} />;
        })}
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
        {parts.map(function (p, i) {
          const v = MOTION.enter(s + p.at, 0.4)(T);
          return (
            <div key={i} style={{
              display: 'flex', gap: 14, alignItems: 'flex-start', opacity: v,
              transform: 'translateX(' + ((1 - v) * -10).toFixed(1) + 'px)',
              borderLeft: '2px solid ' + p.color, paddingLeft: 12,
            }}>
              <span style={{ fontFamily: MONO, fontSize: 13, color: p.color, width: 340, flexShrink: 0, lineHeight: 1.5 }}>{p.label}</span>
              <span style={{ fontSize: 16, color: C.text, flex: 1, lineHeight: 1.5 }}>{p.text}</span>
              <span style={{ fontFamily: MONO, fontSize: 14, color: C.faint, width: 70, textAlign: 'right' }}>{bytes(p.text)} B</span>
            </div>
          );
        })}
      </div>
    </Surface>
  );
}

/* ── the piece ────────────────────────────────────────────────────────── */
function Piece(props) {
  const comp = useComposition();
  const T = comp.T, K = comp.CUES, total = comp.authoredTotal;

  /* camera, in console coordinates (1440×810), zoom relative to the 1.3333 base fit */
  const KEYS = [
    [0, 720, 405, 1.0], [K.Standby + 2.6, 720, 405, 1.05],
    [K.FirstTurn + 0.2, 300, 330, 1.42], [K.Claim + 0.4, 300, 380, 1.5],
    [K.Grounding + 0.3, 900, 470, 1.15], [K.Intervene1 + 0.2, 1160, 300, 1.4],
    [K.Rollback + 0.2, 320, 420, 1.45], [K.BlastRadius + 0.3, 720, 405, 1.0],
    [K.Stale + 0.2, 720, 405, 1.02], [K.Budget + 0.2, 720, 405, 1.02],
    [K.Intervene2 + 0.2, 1160, 320, 1.35], [K.Human + 0.3, 780, 470, 1.4],
    [K.Status + 0.4, 720, 405, 1.05], [total - 1.2, 720, 405, 1.0],
  ];
  let pose = [KEYS[0][1], KEYS[0][2], KEYS[0][3]];
  if (props.camera !== false) {
    for (let i = 1; i < KEYS.length; i++) {
      const k = KEYS[i];
      if (T < k[0]) break;
      const e = MOTION.draw(k[0], k[0] + 1.4)(T);
      pose = [pose[0] + (k[1] - pose[0]) * e, pose[1] + (k[2] - pose[1]) * e, pose[2] + (k[3] - pose[2]) * e];
    }
  }
  const s = 1.33333 * pose[2];
  let tx = 960 - pose[0] * s, ty = 540 - pose[1] * s;
  tx = Math.min(0, Math.max(1920 - 1440 * s, tx));
  ty = Math.min(0, Math.max(1080 - 810 * s, ty));
  const camStyle = {
    position: 'absolute', left: 0, top: 0, width: 1440, height: 810, transformOrigin: '0 0',
    transform: 'translate(' + tx.toFixed(2) + 'px,' + ty.toFixed(2) + 'px) scale(' + s.toFixed(4) + ')',
  };

  /* open on black, close on black — the loop seam */
  const openFade = MOTION.draw(0.35, 1.6)(T);
  const closeFade = 1 - MOTION.draw(total - 1.4, total - 0.15)(T);
  const veil = 1 - Math.min(openFade, closeFade);
  const title = Math.min(MOTION.enter(0.15, 0.9)(T), 1 - MOTION.draw(2.6, 3.6)(T));
  const endTitle = MOTION.enter(total - 2.6, 0.9)(T) * (1 - MOTION.draw(total - 0.55, total - 0.05)(T));

  return (
    <div style={{ position: 'absolute', inset: 0, background: '#05070a', overflow: 'hidden', fontFamily: SANS }}>
      <div style={camStyle}><Console T={T} K={K} /></div>

      {/* analysis surfaces — screen space, above the filmed console */}
      <Shot from={K.Grounding + 0.2} to={K.Intervene1 + 1.4}><GroundingSurface T={T} start={K.Grounding + 0.2} /></Shot>
      <Shot from={K.BlastRadius} to={K.Stale + 0.2}><BlastSurface T={T} start={K.BlastRadius} /></Shot>
      <Shot from={K.Stale + 0.3} to={K.Budget + 0.1}><JustificationSurface T={T} start={K.Stale + 0.3} /></Shot>
      <Shot from={K.Budget + 0.15} to={K.Intervene2 + 1.0}><BudgetSurface T={T} start={K.Budget + 0.15} /></Shot>

      {/* vignette + scanline-free grade */}
      <div style={{ position: 'absolute', inset: 0, pointerEvents: 'none', background: 'radial-gradient(120% 90% at 50% 45%, transparent 45%, rgba(0,0,0,.55) 100%)' }} />

      {/* opening / closing lockup */}
      <div style={{ position: 'absolute', inset: 0, background: '#05070a', opacity: veil, pointerEvents: 'none' }} />
      <div style={{ position: 'absolute', left: 0, right: 0, top: 400, textAlign: 'center', opacity: title, pointerEvents: 'none' }}>
        <div style={{ fontFamily: '"Archivo", ' + SANS, fontSize: 96, fontWeight: 700, letterSpacing: '.22em', color: C.text, textIndent: '.22em' }}>AEGIS</div>
        <div style={{ fontFamily: MONO, fontSize: 17, letterSpacing: '.2em', textTransform: 'uppercase', color: C.faint, marginTop: 18 }}>
          it stays quiet until it shouldn't
        </div>
      </div>
      <div style={{ position: 'absolute', left: 0, right: 0, top: 430, textAlign: 'center', opacity: endTitle, pointerEvents: 'none' }}>
        <div style={{ fontFamily: '"Archivo", ' + SANS, fontSize: 72, fontWeight: 700, letterSpacing: '.22em', color: C.text, textIndent: '.22em' }}>AEGIS</div>
        <div style={{ fontFamily: MONO, fontSize: 15, letterSpacing: '.18em', textTransform: 'uppercase', color: C.accent, marginTop: 20 }}>enter the incident</div>
      </div>

      {props.captions !== false && (
        <div style={{ position: 'absolute', left: 0, right: 0, bottom: 0, paddingBottom: 62, textAlign: 'center', pointerEvents: 'none', background: 'linear-gradient(180deg, transparent, rgba(3,5,8,.82) 42%)' }}>
          <Captions items={[
            { at: 3.4, until: K.FirstTurn + 0.2, text: 'Two engineers on a live bridge. AEGIS is on the call, and nobody talks to it.' },
            { at: K.FirstTurn + 1.4, until: K.Claim, text: 'Ordinary chatter. It says nothing — that is the product working.' },
            { at: K.Claim + 1.4, until: K.Grounding + 0.6, text: 'A number recited from impression.' },
            { at: K.Grounding + 1.2, until: K.Intervene1, text: 'Telemetry reads 91%. The model produced the claim; Python found the contradiction.' },
            { at: K.Intervene1 + 1.4, until: K.Rollback, text: 'The first thing it has said all call.' },
            { at: K.Rollback + 1.6, until: K.BlastRadius + 0.4, text: 'Now a destructive action, resting on a theory nobody confirmed.' },
            { at: K.BlastRadius + 1.4, until: K.BlastRadius + 4.2, text: 'Direct dependents are not the blast radius.' },
            { at: K.BlastRadius + 4.6, until: K.Stale + 0.4, text: 'Six services, three of them user-facing. Nobody said “v2.3” — that came from the topology.' },
            { at: K.Stale + 1.6, until: K.Budget, text: 'Retracting a belief re-opens everything that rested on it.' },
            { at: K.Budget + 0.8, until: K.Intervene2 + 0.4, text: 'One channel, 512 bytes. What gets said is packed exactly, not truncated.' },
            { at: K.Intervene2 + 1.4, until: K.Human + 0.4, text: 'Two findings, one interruption.' },
            { at: K.Human + 1.8, until: K.Status + 0.4, text: 'The human decides. AEGIS records who held it and stops arguing.' },
            { at: K.Status + 1.4, until: total - 2.8, text: 'Open theories are voiced as unconfirmed. Speaking a hypothesis as settled fact is a hard fail.' },
          ]} />
        </div>
      )}
    </div>
  );
}

function AegisHero() {
  const tw = useTweaks(window.TWEAK_DEFAULTS || {});
  const t = tw[0], setTweak = tw[1];
  return (
    <React.Fragment>
      <CompositionStage width={1920} height={1080} bg="#05070a"
        scenes={window.OM_SCENES} playback={window.OM_PLAYBACK}>
        <Piece captions={t.captions} camera={t.cameraMotion} />
      </CompositionStage>
      <TweaksPanel>
        <TweakSection label="Hero" />
        <TweakToggle label="Captions" value={t.captions} onChange={function (v) { setTweak('captions', v); }} />
        <TweakToggle label="Camera moves" value={t.cameraMotion} onChange={function (v) { setTweak('cameraMotion', v); }} />
        <TweakSection label="Editing" />
        <TweakToggle label="Motion editor" value={t.motionEditor} onChange={function (v) { setTweak('motionEditor', v); }} />
      </TweaksPanel>
    </React.Fragment>
  );
}
window.AegisHero = AegisHero;
