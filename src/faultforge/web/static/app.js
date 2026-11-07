// FaultForge's page. It renders only what the API returns, and holds no sample data.
// Every value from the API is untrusted: it reaches the DOM only through h``, which escapes it,
// and mount() is the only place markup enters the document.
"use strict";

/* ---------- escaping ---------- */
function esc(v) { return String(v).replace(/[&<>"'`]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;", "`": "&#96;" }[c])); }
class SafeHtml { constructor(s) { this.s = s; } toString() { return this.s; } }
function part(v) { if (v == null || v === false) return ""; if (v instanceof SafeHtml) return v.s; if (Array.isArray(v)) return v.map(part).join(""); return esc(v); }
function h(strings, ...vals) { let out = strings[0]; vals.forEach((v, i) => { out += part(v) + strings[i + 1]; }); return new SafeHtml(out); }
const raw = s => new SafeHtml(s); // only for constant markup written in this file, never for data
function mount(id, safe) { document.getElementById(id).innerHTML = part(safe); }

/* ---------- icons (constant markup) ---------- */
const I = {
  check: raw('<svg width="14" height="14" viewBox="0 0 16 16" aria-hidden="true"><path d="M3 8.5l3 3 7-7" fill="none" stroke="currentColor" stroke-width="2"/></svg>'),
  x: raw('<svg width="14" height="14" viewBox="0 0 16 16" aria-hidden="true"><path d="M4 4l8 8M12 4l-8 8" fill="none" stroke="currentColor" stroke-width="2"/></svg>'),
  play: raw('<svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><path d="M4 2.5v11l9-5.5z" fill="currentColor"/></svg>'),
  arrow: raw('<svg width="28" height="28" viewBox="0 0 28 28" aria-hidden="true"><path d="M4 14h18M16 7l7 7-7 7" fill="none" stroke="currentColor" stroke-width="2"/></svg>'),
  sun: raw('<svg width="15" height="15" viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="3" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M8 1v2M8 13v2M1 8h2M13 8h2M3 3l1.4 1.4M11.6 11.6 13 13M3 13l1.4-1.4M11.6 4.4 13 3" stroke="currentColor" stroke-width="1.5"/></svg>'),
  shield: raw('<svg width="12" height="12" viewBox="0 0 16 16" aria-hidden="true"><path d="M8 1.5 2.5 3.5v4c0 3.4 2.4 6 5.5 7 3.1-1 5.5-3.6 5.5-7v-4z" fill="currentColor"/></svg>'),
  empty: raw('<svg width="12" height="12" viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="5.5" fill="none" stroke="currentColor" stroke-width="1.6" stroke-dasharray="3 2"/></svg>'),
  stop: raw('<svg width="12" height="12" viewBox="0 0 16 16" aria-hidden="true"><path d="M5 1.5h6L14.5 5v6L11 14.5H5L1.5 11V5z" fill="none" stroke="currentColor" stroke-width="1.8"/><path d="M5.5 8h5" stroke="currentColor" stroke-width="1.8"/></svg>'),
  gauge: raw('<svg width="12" height="12" viewBox="0 0 16 16" aria-hidden="true"><path d="M2 12a6 6 0 1 1 12 0" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M8 12l3-4" stroke="currentColor" stroke-width="1.8"/></svg>'),
  clock: raw('<svg width="14" height="14" viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="6" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M8 4.5V8l2.5 1.5" fill="none" stroke="currentColor" stroke-width="1.5"/></svg>'),
};

/* ---------- fixed text ---------- */
const SITE = {
  headline: "Every request returned success, and the app was still wrong.",
  lead: "Pick a target. FaultForge runs real concurrent load against it in a new sandbox and checks business rules, not just status codes. When a rule breaks, an AI agent can write a fix, and the same load checks whether it holds.",
  footer: "Runs on a Vultr VM. AI calls use Vultr Serverless Inference. Every run gets a new sandbox.",
  repo: "https://github.com/Mikro219/fault-forge",
  repoLabel: "github.com/Mikro219/fault-forge",
};
const AGENT_OFF = {
  not_enabled: "The fix agent isn't enabled on this server.",
  not_configured: "The fix agent has no inference key on this server.",
  daily_usd_cap: "Today's AI budget is used up.",
  daily_token_cap: "Today's AI token limit is used up.",
  total_usd_cap: "The total AI budget is used up.",
  ledger_unavailable: "The AI budget can't be read, so the agent stays off.",
  fix_ip_limit: "You've reached the hourly limit on fixes.",
  fix_ip_daily_limit: "You've reached today's limit on fixes.",
  fix_daily_limit: "Today's limit on fixes was reached.",
};
const CAUSE_LABEL = {
  check_then_act_race: "Check-then-act race",
  lost_update: "Lost update",
  missing_compensation: "Missing compensation",
  required_step_bypassed: "Required step bypassed",
  over_rejection: "Over-rejection",
  concurrency_fault: "Concurrency fault",
};
const INCOMPLETE_TEXT = {
  timeout: "The trial hit its time limit.",
  iterations_short: "Fewer requests finished than planned.",
  engine_exit_nonzero: "The load engine exited with an error.",
  target_restarted: "The target restarted during the trial.",
  reset_failed: "The target's state couldn't be reset.",
  state_snapshot_failed: "The target's state couldn't be read.",
  insufficient_contention: "Too few requests overlapped to test concurrency.",
  request_count_mismatch: "The request count didn't match the plan.",
  budget_exhausted: "The run's budget ran out before this trial.",
};
const GATE_TEXT = {
  comparison_fix_verified: "the comparison with the failing run didn't verify it",
  state_verification: "the database check didn't match",
  code_changed: "the deployed code didn't change",
};
const NOT_VERIFIED_TEXT = {
  trials_incomplete: "trials incomplete",
  build_unchanged: "build unchanged",
  build_unknown: "build unknown",
  stale_source: "stale source",
  not_evaluable: "not evaluable",
};

/* ---------- small helpers ---------- */
const RUN_ID = /^r_[a-f0-9]{16}$/;
const num = v => { const n = Number(v); return Number.isFinite(n) ? n : 0; };
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
const f1 = v => num(v).toFixed(1);
const list = v => Array.isArray(v) ? v : [];
function when(iso) {
  const d = new Date(iso);
  if (iso == null || Number.isNaN(d.getTime())) return String(iso ?? "unknown");
  return d.toLocaleString("en-US", { month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit", timeZoneName: "short" });
}
function usd(v) { return "$" + num(v).toFixed(4); }
function seconds(fromIso, toIso) {
  const ms = new Date(toIso).getTime() - new Date(fromIso).getTime();
  if (!Number.isFinite(ms) || ms < 0) return "";
  return ms < 1000 ? "under 1 s" : `${Math.round(ms / 1000)} s`;
}
function shortHash(hash) { const text = String(hash); return text.length > 16 ? `${text.slice(0, 15)}…${text.slice(-4)}` : text; }
const pillFor = v => { const c = { FAIL: "fail", PASS: "pass", INCONCLUSIVE: "incon" }[v] || "none"; return h`<span class="pill ${c}">${v || "No verdict"}</span>`; };
const statusPill = s => s === "fail" ? h`<span class="pill fail">${I.x} Failed</span>` : s === "pass" ? h`<span class="pill pass">${I.check} Held</span>` : h`<span class="pill none">Not evaluated</span>`;
const okMark = b => b ? h`<span class="pill pass">${I.check} Yes</span>` : h`<span class="pill fail">${I.x} No</span>`;
const step = (no, title, aside) => h`<div class="step"><span class="no">Step ${no}</span><h2>${title}</h2>${aside ? h`<span class="aside">${aside}</span>` : ""}</div>`;
const trialsLine = result => result.completeness ? `${result.completeness.trials_completed}/${result.completeness.trials_planned} trials complete` : "No trials complete";
const targetName = id => (S.status && list(S.status.targets).find(t => t.id === id) || {}).title || id;

/* ---------- state ---------- */
const S = {
  status: null, statusFailed: false, replays: [], target: null,
  view: "start", current: null, replayMode: false,
  notice: null, cooldownUntil: 0, fixNotice: null, pollNote: null,
};
let pollToken = 0;
let lastHtml = "";

async function api(path, options) {
  try {
    const response = await fetch(path, options);
    let body = {};
    try { body = await response.json(); } catch (e) { body = {}; }
    return { ok: response.ok, status: response.status, body, retryAfter: num(response.headers.get("Retry-After")) };
  } catch (e) {
    return { ok: false, status: 0, body: { reason: "network", message: "The server can't be reached." }, retryAfter: 0 };
  }
}

/* ---------- 1 · start, with busy, cooldown and budget ---------- */
function budgetNotice(status) {
  if (status.agent_enabled || !status.budget.enabled || !status.budget.exhausted) return "";
  const reason = status.agent_disabled_reason;
  const b = status.budget;
  const title = reason === "total_usd_cap" || reason === "ledger_unavailable" ? "The live agent is off; replays still work" : "The live agent is off for today; replays still work";
  return h`<div class="notice info" role="status" id="budget-notice"><b>${title}</b><span class="fg2">${AGENT_OFF[reason] || "The AI budget is used up."} Spent today: ${usd(b.daily_usd_spent)} of ${usd(b.daily_usd_cap)}. Load tests still run, and the recorded runs below show the full flow.</span></div>`;
}
function startNotices(status) {
  const out = [];
  if (S.notice) out.push(h`<div class="notice warn" role="alert" id="refusal"><b>${I.clock} ${S.cooldownUntil > Date.now() ? `Try again in ${Math.ceil((S.cooldownUntil - Date.now()) / 1000)} s` : S.notice.title}</b><span class="fg2">${S.notice.message}</span></div>`);
  if (status && status.busy) {
    const full = status.queue_length >= status.queue_limit;
    out.push(h`<div class="notice warn" role="status" id="busy-notice"><b>${I.clock} ${full ? "The queue is full" : "Another run is in progress"}</b><span class="fg2">${full ? "Try again soon, or watch a replay." : "If you start a run, it waits for the sandbox to be free. This page updates on its own."}</span><span><span class="pill warn">Queue ${status.queue_length} of ${status.queue_limit}</span></span></div>`);
  }
  if (status) out.push(budgetNotice(status));
  return out;
}
function replaySummary(r) {
  if (r.fix_verified) return "Fix verified";
  if (r.has_fix) return "The agent tried a fix, and it wasn't verified";
  if (r.target === "hostile-build") return "Containment test";
  return { FAIL: "Violation proven", PASS: "Invariants held", INCONCLUSIVE: "Not enough evidence. This isn't a pass." }[r.verdict] || "No verdict";
}
function renderStart() {
  const status = S.status;
  const targets = status ? list(status.targets) : [];
  const chosen = targets.some(t => t.id === S.target) ? S.target : (targets[0] || {}).id;
  const cooling = S.cooldownUntil > Date.now();
  const full = status && status.queue_length >= status.queue_limit;
  const hasReplays = S.replays.length > 0;
  const btn = cooling ? h`<button class="runbtn" id="run" disabled>${I.clock} Run available in ${Math.ceil((S.cooldownUntil - Date.now()) / 1000)} s</button>`
    : full ? h`<button class="runbtn" id="run" disabled>${I.clock} Queue full</button>`
    : h`<button class="runbtn" id="run" data-act="run" ${status ? "" : raw("disabled")}>${I.play} Run</button>`;
  return h`
  <section class="panel hero fade-in">
    <h1>${SITE.headline}</h1>
    <p class="lead">${SITE.lead}</p>
    ${step("01", "Choose a target")}
    ${status ? h`<fieldset class="targets"><legend class="sr">Target</legend>
      ${targets.map(t => h`<label class="target"><input type="radio" name="target" id="target-${t.id}" value="${t.id}" data-act="pick" ${t.id === chosen ? raw("checked") : ""}>
        <span class="nm">${t.title}</span><span class="ds">${t.description}</span>
        <span class="meta"><span class="chip">Changes data in a throwaway sandbox.</span></span></label>`)}
    </fieldset>` : S.statusFailed ? h`<div class="notice fail" role="alert"><b>The server can't be reached</b><span class="fg2">This page keeps trying.</span></div>` : h`<p class="muted">Loading the targets…</p>`}
    ${startNotices(status)}
    <div class="start-actions">${btn}${hasReplays && (cooling || full || (status && status.budget.exhausted)) ? h`<button class="runbtn alt" data-act="replay" data-id="${S.replays[0].id}">${I.play} Watch a replay</button>` : ""}<span class="muted small">You can't upload code or change settings.</span></div>
  </section>
  ${hasReplays ? h`<section class="panel">
    <div class="ph"><h2>Past runs</h2><span class="sp muted small">recorded real runs, played back as replays</span></div>
    <div class="replays">${S.replays.map(r => h`<div class="replay-row">
      <span class="pill info">Replay</span><span class="hide-sm">${pillFor(r.verdict)}</span>
      <span><b>${targetName(r.target)}</b> <span class="fg2">· ${replaySummary(r)}</span></span>
      <span class="mono muted small hide-sm">${when(r.recorded_at)}</span>
      <button class="btn sm" data-act="replay" data-id="${r.id}" aria-label="Watch the replay of ${targetName(r.target)}, ${when(r.recorded_at)}">Watch</button></div>`)}</div>
  </section>` : ""}`;
}

function runHead(st) {
  return h`<section class="panel runhead"><b>${targetName(st.target)}</b>
    <span>run <span class="mono">${st.id}</span></span>
    <span>${st.replay ? "recorded" : "started"} <span class="mono">${when(st.replay ? st.recorded_at : st.started_at || st.created_at)}</span></span></section>`;
}

/* ---------- 2 · running: only the stages events can show ---------- */
function stagesFor(st) {
  const at = name => (list(st.events).find(e => e.name === name) || {}).at;
  const queued = at("queued"), started = at("sandbox_started"), finished = at("finished") || at("failed") || at("shutdown");
  const failedRun = st.stage === "failed" || (st.result && st.result.status === "failed");
  const stage = (label, state, time, detail) => ({ label, state, time, detail });
  const stages = [];
  if (started) stages.push(stage("Queued", "done", seconds(queued, started), "Waited for the sandbox to be free."));
  else if (failedRun) stages.push(stage("Queued", "failed", "", (st.result && st.result.verdict_summary) || "The run ended before it started."));
  else stages.push(stage("Queued", "active", "", st.queue_position ? `Position ${st.queue_position} in the queue.` : "Waiting for the sandbox to be free."));
  if (!started) stages.push(stage("Load test in a new sandbox", "pending", "", ""));
  else if (failedRun) stages.push(stage("Load test in a new sandbox", "failed", seconds(started, finished), (st.result && st.result.verdict_summary) || "The run failed."));
  else if (finished) stages.push(stage("Load test in a new sandbox", "done", seconds(started, finished), "Finished."));
  else stages.push(stage("Load test in a new sandbox", "active", "", "The server reports no progress inside this stage, so none is drawn here."));
  stages.push(stage("Verdict", failedRun ? "pending" : finished ? "done" : "pending", "", ""));
  return stages;
}
function renderStages(stages) {
  return h`<div class="stages">${stages.map(s => {
    const st = s.state;
    const ic = st === "done" ? h`<span class="pass-ic">${I.check}</span>` : st === "active" ? raw('<span class="spin" aria-hidden="true"></span>') : st === "failed" ? h`<span class="fail-ic">${I.x}</span>` : raw('<span class="hollow" aria-hidden="true"></span>');
    const word = { done: "done", active: "in progress", failed: "failed", pending: "not started" }[st];
    return h`<div class="stage ${st}"><span class="ic">${ic}</span><span class="nm">${s.label} <span class="sr">(${word})</span></span><span class="tm">${st === "active" ? "now" : st === "failed" && !s.time ? "stopped" : s.time}</span>${s.detail ? h`<span class="dt">${s.detail}</span>` : ""}</div>`;
  })}</div>`;
}
function renderRunning(st) {
  return h`${runHead(st)}${step("02", "Load test")}
  <section class="panel fade-in" id="stages" aria-live="polite"><div class="ph"><h2>Stages</h2><span class="sp"><span class="pill info">${st.stage === "queued" ? "Queued" : "Running"}</span></span></div>${renderStages(stagesFor(st))}</section>
  ${pollNote()}`;
}
const pollNote = () => S.pollNote ? h`<div class="notice warn" role="status"><b>${S.pollNote}</b></div>` : "";

/* ---------- 10 · a failed run ---------- */
function renderFailedRun(st) {
  const result = st.result;
  const reason = result.failure ? result.failure.message : result.verdict_summary;
  return h`${runHead(st)}${step("02", "Load test")}
  <section class="panel fade-in"><div class="ph"><h2>Stages</h2><span class="sp"><span class="pill warn">${I.x} Run failed</span></span></div>${renderStages(stagesFor(st))}</section>
  <div class="notice fail" role="alert" id="run-failed"><b>This run ended without a verdict</b><span class="fg2">${reason}</span><span class="muted">It says nothing about whether the target is correct. Try again in a few minutes.</span></div>
  <div><button class="btn" data-act="home">Back to start</button></div>`;
}

/* ---------- evidence ---------- */
function timeline(tl, performance) {
  const rows = list(tl.points);
  if (!rows.length) return "";
  const end = v => num(v.offset_ms) + num(v.duration_ms);
  const maxEnd = Math.max(1, ...rows.map(end));
  const stepMs = [5, 10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000, 5000, 10000, 20000, 50000].find(s => maxEnd / s <= 10) || 100000;
  const xMax = Math.ceil(maxEnd / stepMs) * stepMs;
  const dense = rows.length > 40;
  const W = 1000, L = dense ? 16 : 60, R = 16, rh = dense ? 4 : 11, gap = dense ? 1 : 4, top = 14, H = top + rows.length * (rh + gap) + 28;
  const x = v => L + (num(v) / xMax) * (W - L - R);
  const parts = [];
  for (let v = 0; v <= xMax; v += stepMs) parts.push(h`<line class="t-grid" x1="${f1(x(v))}" x2="${f1(x(v))}" y1="${top - 6}" y2="${H - 24}"/><text class="t-lbl" x="${f1(x(v))}" y="${H - 8}" text-anchor="middle">${v}${v === 0 ? " ms" : ""}</text>`);
  rows.forEach((r, i) => {
    const y = top + i * (rh + gap);
    const cls = r.outcome === "accepted" ? "t-acc" : r.outcome === "rejected" ? "t-rej" : "t-unx";
    if (!dense) parts.push(h`<text class="t-lbl" x="${L - 8}" y="${y + rh - 2}" text-anchor="end">#${String(i + 1).padStart(2, "0")}</text>`);
    parts.push(h`<rect class="${cls}" x="${f1(x(r.offset_ms))}" y="${y}" width="${f1(Math.max(2, x(end(r)) - x(r.offset_ms)))}" height="${rh}"><title>request ${i + 1} · ${r.outcome} · ${r.duration_ms} ms</title></rect>`);
  });
  const count = fn => rows.filter(fn).length;
  const ok = count(r => r.outcome === "accepted"), rej = count(r => r.outcome === "rejected"), other = rows.length - ok - rej;
  const lat = performance && performance.latency_ms;
  const latText = lat && lat.p50 != null && lat.p95 != null ? `p50 ${Math.round(num(lat.p50))} ms · p95 ${Math.round(num(lat.p95))} ms · ` : "";
  return h`<section class="panel" id="timeline"><div class="ph"><h2>Request timeline · trial ${tl.trial}</h2><span class="sp lat">${latText}latency doesn't affect the verdict</span></div>
    <div class="pb stack">
      <div class="legend"><span class="lg"><span class="sw acc"></span>Accepted (${ok})</span><span class="lg"><span class="sw rej"></span>Rejected, expected (${rej})</span><span class="lg"><span class="sw unx"></span>Unexpected or transport error (${other})</span></div>
      <div class="tl-wrap" tabindex="0" aria-label="Request timeline, scrollable"><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Request timeline: ${ok} accepted, ${rej} rejected, ${other} other">${parts}</svg></div>
      ${tl.truncated ? h`<p class="muted small">An evenly spaced sample of ${rows.length} of ${tl.total_requests} requests.</p>` : ""}
    </div></section>`;
}

function renderLedger(result) {
  const c = result.contrast;
  if (!c) return "";
  const n = num(c.accepted), known = c.stock_before != null, stock = num(c.stock_before), oversold = known && n > stock;
  const shown = Math.min(n, 200);
  const stubs = Array.from({ length: shown }, (_, i) => i < stock ? h`<span class="stub real">Ticket</span>` : h`<span class="stub over" aria-hidden="true">×</span>`);
  const countsSource = c.counts_source === "measured_by_runner" ? "measured by the runner" : c.counts_source;
  const stateSource = c.state_source === "reported_by_target" ? "reported by the target" : c.state_source;
  const others = [plural(num(c.rejected), "rejection"), `${num(c.unexpected)} unexpected`, plural(num(c.transport_error), "transport error")].join(", ");
  return h`<section class="panel" id="contrast"><div class="ph"><h2>What the app said vs. what happened</h2><span class="sp muted small">trial ${c.trial}</span></div>
  <div class="pb ledger">
    ${known ? h`<div class="ledger-head"><span class="big">${n} accepted${oversold ? h`<span class="ne" aria-label="but only">≠</span>` : h`<span class="muted"> · </span>`}${stock} in stock</span></div>
    <div class="stubs" role="img" aria-label="${n} confirmations: ${Math.min(n, stock)} backed by a real ticket, ${Math.max(0, n - stock)} with no ticket behind them">${stubs}</div>
    ${n > shown ? h`<p class="muted small">Showing ${shown} of ${n}.</p>` : ""}
    <div class="stub-key"><span><span class="stub real key"></span>backed by stock (${Math.min(n, stock)})</span><span><span class="stub over key"></span>confirmed with no ticket behind it (${Math.max(0, n - stock)})</span></div>` : ""}
    <div class="facts">
      <div class="fact"><span class="k">Accepted</span><span class="v ${oversold ? "bad" : ""}">${n}</span><span class="n">requests accepted, with ${others}</span><span><span class="chip src">${countsSource}</span></span></div>
      ${known ? h`<div class="fact"><span class="k">In stock</span><span class="v">${stock}</span><span class="n">before the trial</span><span><span class="chip src">${stateSource}</span></span></div>` : ""}
      ${c.orders != null ? h`<div class="fact"><span class="k">Recorded</span><span class="v">${plural(num(c.orders), "order")}</span><span class="n">${c.stock_after != null ? `stock reads ${c.stock_after} afterwards` : "during the trial"}</span><span><span class="chip src">${stateSource}</span></span></div>` : ""}
    </div>
  </div></section>`;
}

function invariantSummary(result) {
  const rows = new Map();
  list(result.trials).filter(t => t.kind === "concurrent" && t.status !== "skipped").forEach(t => {
    list(t.invariants).forEach((item, i) => {
      const key = item.name == null ? `#${i}` : item.name;
      const row = rows.get(key) || { name: item.name, pass: 0, fail: 0, other: 0 };
      if (item.result === "pass") row.pass++; else if (item.result === "fail") row.fail++; else row.other++;
      rows.set(key, row);
    });
  });
  rows.forEach(row => { row.status = row.fail ? "fail" : row.pass ? "pass" : "other"; row.trials = row.pass + row.fail + row.other; });
  return rows;
}
const observedText = o => Object.entries(o || {}).map(([k, v]) => `${k} = ${v}`).join(" · ");
const violationLabel = type => String(type).replace(/^[a-z_]+\./, "").replace(/_/g, " ");

function renderProvenSuspected(result) {
  const rows = invariantSummary(result);
  const violations = list(result.violations);
  const unnamed = violations.filter(v => v.invariant == null || !rows.has(v.invariant));
  const serial = list(result.trials).find(t => t.kind === "serial_control" && t.status === "completed");
  const causes = list(result.suspected_causes);
  return h`<div class="ps">
    <section class="panel" id="proven"><div class="ph"><h2>${I.check} Proven</h2><span class="sp muted small">measured in this run, checked after every trial</span></div>
      ${[...rows.values()].map(p => {
        const proof = violations.find(v => v.invariant === p.name && p.name != null);
        const line = p.status === "fail" ? `failed in ${p.fail}/${p.trials} trials` : p.status === "pass" ? `held in ${p.pass}/${p.trials} trials` : `not evaluated in ${p.other}/${p.trials} trials`;
        return h`<div class="inv-row ${p.status}">${statusPill(p.status)}<span><span class="nm">${p.name == null ? "Unnamed invariant" : p.name}</span></span>
        <span>${proof ? h`<div class="obs fail">${observedText(proof.observed)}</div>` : ""}<div class="tr">${line}</div></span></div>`;
      })}
      ${unnamed.map(v => h`<div class="inv-row fail">${statusPill("fail")}<span><span class="nm">${violationLabel(v.type)}</span></span>
        <span><div class="obs fail">${observedText(v.observed)}</div><div class="tr">failed in ${v.trials_violated} trials</div></span></div>`)}
    </section>
    <section class="panel suspected" id="suspected"><div class="ph"><h2>Suspected · not proven</h2></div>
      <div class="pb stack">
        ${causes.length ? causes.map(cause => h`<div class="stack"><p class="claim">${CAUSE_LABEL[cause.cause] || cause.cause}</p><p class="fg2">${cause.text}</p></div>`) : h`<p class="fg2">No suspected cause was generated.</p>`}
        ${serial ? h`<div class="eyebrow">Supported by</div>
        <ul><li>Serial control: the same requests sent one at a time gave ${serial.accepted} accepted and ${serial.rejected} rejected.</li></ul>` : ""}
        <p class="must">Root cause must be confirmed in code.</p>
      </div>
    </section>
  </div>`;
}

function verdictBlock(v, trials, title, sub, extraCls) {
  return h`<section class="verdict fade-in" id="verdict"><div class="v-block ${v} ${extraCls || ""}">${v}${extraCls === "unverified" ? h`<small>${I.x} Not verified</small>` : ""}</div>
    <div class="v-body"><div class="v-meta"><span class="trials">${trials}</span></div><h1>${title}</h1>${sub ? h`<p class="sub">${sub}</p>` : ""}</div></section>`;
}
function renderDisclosures(result) {
  const items = list(result.disclosures);
  return items.length ? h`<ul class="disclosures" aria-label="Disclosures">${items.map(d => h`<li>${d}</li>`)}</ul>` : "";
}

/* ---------- 3 · FAIL ---------- */
function failHeadline(result) {
  const c = result.contrast;
  if (c && c.stock_before != null && num(c.accepted) > num(c.stock_before)) return `${c.accepted} requests were accepted for ${plural(num(c.stock_before), "ticket")}.`;
  return result.verdict_summary;
}
function failSub(result) {
  const c = result.contrast;
  if (!c || num(c.rejected) || num(c.unexpected) || num(c.transport_error)) return "";
  return `Every request got a success response${c.stock_after != null && num(c.stock_after) >= 0 ? `, and the stock counter reads ${c.stock_after}` : ""}, so the app looks healthy. It wasn't.`;
}
const evidenceStack = result => h`${renderLedger(result)}${renderProvenSuspected(result)}${result.timeline ? timeline(result.timeline, result.performance) : ""}${renderDisclosures(result)}`;
function renderFailFull(result) {
  return h`${verdictBlock("FAIL", trialsLine(result), failHeadline(result), failSub(result))}${evidenceStack(result)}`;
}
function renderFailCompact(result) {
  return h`<details class="evidence" data-key="evidence"><summary><div class="verdict compact" id="verdict"><div class="v-block FAIL">FAIL</div>
    <div class="v-body"><div class="v-meta"><span class="trials">${trialsLine(result)}</span><span class="toggle btn sm"></span></div><h3>${failHeadline(result)}</h3></div></div></summary>
    <div class="stack">${evidenceStack(result)}</div></details>`;
}

/* ---------- 4, 5 · the agent ---------- */
function parseDiff(text) {
  const rows = [];
  let adds = 0, dels = 0;
  String(text).split("\n").forEach(line => {
    if (line.startsWith("+++") || line.startsWith("---")) return;
    if (line.startsWith("@@")) { rows.push(["@", line]); return; }
    const k = line[0] === "+" || line[0] === "-" ? line[0] : " ";
    if (k === "+") adds++;
    if (k === "-") dels++;
    rows.push([k, line.slice(1)]);
  });
  return { rows, adds, dels };
}
function costLine(a, model, solo) {
  const tokens = a.usage_reported ? `${num(a.input_tokens).toLocaleString("en-US")} in · ${num(a.output_tokens).toLocaleString("en-US")} out` : "token use not reported";
  return h`<div class="cost ${solo ? "solo" : ""}"><span>model ${a.model || model}</span><span>${tokens}</span><span>${usd(a.cost_usd)}</span></div>`;
}
function diffView(a, model) {
  const d = parseDiff(a.diff);
  return h`<div class="diff"><div class="diff-h"><span>app.py</span><span class="pass-t">+${d.adds}</span><span class="fail-t">−${d.dels}</span><span class="sp"><span class="chip ai">AI-written code. Runs only in the sandbox.</span></span></div>
    <div class="diff-body" tabindex="0" aria-label="Diff of attempt ${a.attempt}"><table><tbody>${d.rows.map(([k, code]) => h`<tr class="${k === "+" ? "add" : k === "-" ? "del" : k === "@" ? "hunk" : ""}"><td class="g">${k === "+" || k === "-" ? k : ""}<span class="sr">${k === "+" ? "added" : k === "-" ? "removed" : ""}</span></td><td>${code}</td></tr>`)}</tbody></table></div>
    ${a.diff_truncated ? h`<div class="diff-note">The diff is cut off here because it's very long.</div>` : ""}
    ${costLine(a, model)}</div>`;
}
function attemptResult(a, more) {
  if (a.passed) return { headline: `Attempt ${a.attempt} passed the re-test`, detail: a.candidate && a.candidate.contrast ? `Same workload: ${a.candidate.contrast.accepted} accepted and ${a.candidate.contrast.rejected} rejected.` : "" };
  const c = a.candidate;
  const parts = [];
  if (c && c.contrast && c.status === "completed") parts.push(`Same workload: ${c.contrast.accepted} accepted and ${c.contrast.rejected} rejected.`);
  if (c && list(c.violations).length) parts.push(`${plural(c.violations.length, "proven violation")}.`);
  const gates = list(a.gate_failed).map(g => GATE_TEXT[g]).filter(Boolean);
  if (gates.length) parts.push(`It stopped because ${gates.join(", and ")}.`);
  return { headline: `Attempt ${a.attempt} rejected: ${a.failure_text || "this attempt failed."}`, detail: parts.join(" "), next: more ? "The agent gets this result and tries again." : "" };
}
function attemptBody(a, fix, more) {
  const res = attemptResult(a, more);
  const rules = list(a.policy_violations);
  const result = a.passed ? h`<div class="result ok"><b>${res.headline}</b><span class="fg2">${res.detail}</span></div>`
    : h`<div class="result rejected"><b>${res.headline}</b>${res.detail ? h`<span class="fg2">${res.detail}</span>` : ""}${rules.length ? h`<div class="rules"><ul>${rules.map(t => h`<li><span class="pill fail">${I.x} Broken</span><span>${t}</span></li>`)}</ul></div>` : ""}${res.next ? h`<span class="muted small">${res.next}</span>` : ""}</div>`;
  return h`<div class="pb stack">${a.diff ? diffView(a, fix.model) : costLine(a, fix.model, true)}</div>${result}`;
}
function attemptTag(a) {
  if (a.passed) return h`<span class="pill pass">${I.check} Passed re-test</span>`;
  if (a.failure_reason === "policy_violation") return h`<span class="pill none">${I.x} Rejected before running</span>`;
  if (a.candidate) return h`<span class="pill none">${I.x} Rejected by re-test</span>`;
  return h`<span class="pill none">${I.x} Rejected</span>`;
}
function attemptCard(a, fix, opts) {
  const more = (opts && opts.more) || false;
  const max = fix.max_attempts;
  if (opts && opts.collapsed) {
    return h`<details class="attempt" data-key="attempt-${a.attempt}"><summary class="attempt-h"><b>Attempt ${a.attempt} of ${max}</b>${attemptTag(a)}<span class="fg2 small">${attemptResult(a, more).headline}</span></summary>${attemptBody(a, fix, more)}</details>`;
  }
  return h`<section class="attempt fade-in" data-attempt="${a.attempt}"><div class="attempt-h"><b>Attempt ${a.attempt} of ${max}</b><span class="chip">${a.model || fix.model}</span>${attemptTag(a)}</div>${attemptBody(a, fix, more)}</section>`;
}
const agentIntro = fix => step("03", "Agent fix", `Up to ${fix ? fix.max_attempts : 3} attempts · each one is re-tested with the same workload`);

function fixCta(st) {
  const status = S.status;
  if (st.replay) return "";
  if (st.fix_available) {
    return h`<section class="panel cta" id="fix-cta"><button class="runbtn" data-act="fix">Let the agent fix it</button>
    <span class="fg2">The agent gets the proven failures and the app's source, and its code runs only in a new sandbox.</span></section>${S.fixNotice ? h`<div class="notice warn" role="alert"><b>${S.fixNotice}</b></div>` : ""}`;
  }
  const reason = status && !status.agent_enabled
    ? (AGENT_OFF[status.agent_disabled_reason] || "The fix agent is off.")
    : "This run can't be fixed any more: its evidence is no longer kept.";
  return h`<section class="panel cta" id="fix-cta"><button class="runbtn" disabled>Let the agent fix it</button><span class="fg2">${reason} Replays still work.</span></section>`;
}
function renderFailState(st) {
  return h`${runHead(st)}${step("02", "Load test")}${renderFailFull(st.result)}${st.replay ? "" : agentIntro(st.fix)}${fixCta(st)}`;
}
function workingCard(fix, st) {
  const number = list(fix.attempts).length + 1;
  const started = list(st.events).find(e => e.name === "fix_started");
  const queued = fix.status === "queued";
  return h`<section class="attempt fade-in" id="agent-working" aria-live="polite"><div class="attempt-h"><b>${queued ? "Waiting for the sandbox" : `Attempt ${number} of ${fix.max_attempts}`}</b><span class="chip">${fix.model}</span><span class="pill info">${queued ? "Queued" : "In progress"}</span>${started && !queued ? h`<span class="sp muted small mono">started ${when(started.at)}</span>` : ""}</div>
    <div class="wait"><p>${queued ? "The fix starts when the sandbox is free." : "Writing a fix from the failing evidence, then re-testing it in a new sandbox."}</p><div class="breath" aria-hidden="true"></div><p class="muted small">Nothing runs until the change is written and checked against the code rules.</p></div></section>`;
}
function renderFixState(st) {
  const fix = st.fix;
  const attempts = list(fix.attempts);
  const working = fix.status === "queued" || fix.status === "running";
  const head = h`${runHead(st)}${step("02", "Load test")}${renderFailCompact(st.result)}${agentIntro(fix)}`;
  if (working) return h`${head}${attempts.map(a => attemptCard(a, fix, { more: true }))}${workingCard(fix, st)}${pollNote()}`;
  const chosen = attempts.find(a => a.passed) || attempts[attempts.length - 1];
  const earlier = attempts.filter(a => a !== chosen);
  const passed = chosen && chosen.candidate && chosen.candidate.verdict === "PASS";
  const cards = h`${earlier.map(a => attemptCard(a, fix, { collapsed: true, more: true }))}${chosen ? attemptCard(chosen, fix) : ""}`;
  const gate = chosen ? gateFor(st.result, fix, chosen) : null;
  if (gate && gate.verified) return h`${head}${cards}${renderVerify(st.result, chosen, gate)}${backRow()}`;
  return h`${head}${cards}${passed ? renderVerify(st.result, chosen, gate) : ""}${step(passed ? "05" : "04", "Result")}${renderHonest(st.result, fix)}`;
}
const backRow = () => h`<div><button class="btn" data-act="home">Start a new run</button></div>`;

/* ---------- 6 · verification: "Fix verified" appears only when every part of the gate holds ---------- */
function gateFor(baseline, fix, attempt) {
  const c = attempt.candidate;
  if (!c) return { verified: false, workload: false, code: false, database: false };
  const workload = Boolean(baseline.config_hash) && baseline.config_hash === c.config_hash;
  const code = attempt.code_changed === true && Boolean(baseline.deployed_code_sha256) && Boolean(c.deployed_code_sha256) && baseline.deployed_code_sha256 !== c.deployed_code_sha256;
  const database = Boolean(c.state_verification) && c.state_verification.status === "matched";
  const comparison = c.comparison;
  const verified = fix.status === "verified" && attempt.passed === true && c.status === "completed" && c.verdict === "PASS"
    && c.fix_verified === true && Boolean(comparison) && comparison.fix_verified === true && comparison.status === "COMPARABLE"
    && list(c.violations).length === 0 && workload && code && database;
  return { verified, workload, code, database };
}
function databaseEvidence(c) {
  const sv = c.state_verification || { status: "unavailable", mismatches: [] };
  if (sv.status === "matched") return "matched: the database, read directly by the host for the final trial, agrees with what the target reported.";
  if (sv.status === "mismatch") return `mismatch: ${list(sv.mismatches).map(m => `${m.name}: reported ${m.reported ?? "nothing"}, database ${m.database ?? "nothing"}`).join("; ") || "the values differ"}.`;
  return `${sv.status}: the database couldn't be compared${sv.reason ? ` (${sv.reason})` : ""}.`;
}
function renderVerify(baseline, attempt, gate) {
  const c = attempt.candidate;
  const verified = gate.verified;
  const n = c.completeness ? c.completeness.trials_completed : 0;
  const sv = c.state_verification ? c.state_verification.status : "unavailable";
  const evidence = [
    { ok: gate.workload, title: gate.workload ? "Same workload fingerprint" : "Workload fingerprint doesn't match", value: `${baseline.config_hash ? shortHash(baseline.config_hash) : "not recorded"} before, ${c.config_hash ? shortHash(c.config_hash) : "not recorded"} after` },
    { ok: gate.code, title: gate.code ? "New code deployed" : "No proof of new code", value: `Deployed code hash, by the host: ${baseline.deployed_code_sha256 ? shortHash(baseline.deployed_code_sha256) : "not recorded"} → ${c.deployed_code_sha256 ? shortHash(c.deployed_code_sha256) : "not recorded"}` },
    { ok: gate.database, title: gate.database ? "Database check matched" : "Database check did not match", value: databaseEvidence(c) },
  ];
  const headline = verified ? `0 violations in ${n} trials under the same workload.` : sv === "mismatch" ? "The responses passed, but the database disagrees. This fix is not verified." : "The responses passed, but the gate didn't hold. This fix is not verified.";
  const sub = verified ? "" : "Treat this as unfixed: a fix that changes what the service says without changing what it does can look exactly like this.";
  const badge = h`<section class="badge ${verified ? "" : "bad"}" id="fix-badge"><div class="seal">${verified ? I.check : I.x} ${verified ? "Fix verified" : "Not verified"}</div>
    <div class="ev">${evidence.map(e => h`<div class="ev-row ${e.ok ? "" : "bad"}">${okMark(e.ok)}<b>${e.title}</b><span class="val">${e.value}</span></div>`)}</div></section>`;
  const before = baseline.contrast, after = c.contrast;
  const card = (verdict, label, contrast, extra) => h`<div class="cmp-card"><div class="v-block ${verdict} ${extra || ""}">${verdict}${extra ? h`<small>${I.x} Not verified</small>` : ""}</div><div class="body"><span class="role">${label}</span><span class="big">${contrast ? `${contrast.accepted} accepted` : ""}</span><span class="fg2 mono">${contrast ? `${contrast.rejected} rejected` : ""}</span></div></div>`;
  const beforeRows = invariantSummary(baseline), afterRows = invariantSummary(c);
  const obs = row => !row ? "" : row.fail ? `failed ${row.fail}/${row.trials}` : `held ${row.pass}/${row.trials}`;
  const changeTag = text => h`<span class="change ch-${String(text).replace(/ /g, "-")}">${text}</span>`;
  const invariants = c.comparison ? list(c.comparison.invariants) : [];
  const deltas = before && after ? [["Accepted", before.accepted, after.accepted], ["Rejected", before.rejected, after.rejected], ["Violations", list(baseline.violations).length, list(c.violations).length]] : [];
  return h`${step("04", "Verification", "same workload as the failing run")}
  ${verdictBlock("PASS", trialsLine(c), headline, sub, verified ? "" : "unverified")}
  ${badge}
  <div class="cmp-hero">${card("FAIL", "Before fix", before)}<div class="cmp-arrow">${I.arrow}</div>${card("PASS", `After fix · attempt ${attempt.attempt}`, after, verified ? "" : "unverified")}</div>
  ${deltas.length ? h`<div class="deltas"><span class="eyebrow">Same workload</span>${deltas.map(([k, a, b]) => h`<span class="delta-chip"><span class="k">${k}</span><span class="from">${a}</span>${b}</span>`)}</div>` : ""}
  ${invariants.length ? h`<section class="panel" id="compare"><div class="ph"><h2>Invariants</h2></div>
    <div class="tbl-wrap" tabindex="0" aria-label="Invariants before and after, scrollable"><table class="t"><thead><tr><th>Invariant</th><th>Before</th><th>After</th><th class="r">Change</th></tr></thead><tbody>
    ${invariants.map(i => {
      const b = beforeRows.get(i.name), a = afterRows.get(i.name);
      const label = i.result === "FIXED" && !verified ? "NOT VERIFIED" : String(i.result).replace(/_/g, " ");
      return h`<tr><td class="nmcell"><b>${i.name == null ? "Unnamed invariant" : i.name}</b>${i.reason ? h`<div class="obsline">${NOT_VERIFIED_TEXT[i.reason] || i.reason}</div>` : ""}</td><td>${statusPill(b && b.status)}<div class="obsline">${obs(b)}</div></td><td>${statusPill(a && a.status)}<div class="obsline">${obs(a)}</div></td><td class="r">${changeTag(label)}</td></tr>`;
    })}
    </tbody></table></div></section>` : ""}
  ${verified && c.timeline ? timeline(c.timeline, c.performance) : ""}
  ${renderDisclosures(c)}
  <p class="foot-note">${verified ? `This shows the invariants held for this workload, these ${n} trials and this code. It isn't proof that the race can never happen.` : "The invariants above were checked from responses and the target's own reports. They don't outweigh a failed check."}</p>`;
}

/* ---------- 7 · honest failure ---------- */
function renderHonest(baseline, fix) {
  const exhausted = fix.stop_reason === "attempts_exhausted";
  return h`<section class="honest fade-in" id="honest"><span class="eyebrow">Agent result</span>
    <h3>${exhausted ? `The agent couldn't fix this in ${fix.max_attempts} attempts.` : "The agent stopped without a verified fix."}</h3>
    <p class="fg2">${fix.stop_text || ""} The original failure still stands: ${failHeadline(baseline)}</p>
    <p class="muted">The suspected cause hasn't changed. A person should confirm it in code. This fix cost ${usd(fix.total_cost_usd)} in AI calls.</p>
    <div><button class="btn" data-act="home">Start a new run</button></div></section>`;
}

/* ---------- 8 · INCONCLUSIVE ---------- */
function renderInconclusiveState(st) {
  const result = st.result;
  const trialPill = t => t.status === "completed" ? h`<span class="pill pass">${I.check} Complete</span>` : t.status === "incomplete" ? h`<span class="pill warn">${I.clock} Incomplete</span>` : h`<span class="pill none">Skipped</span>`;
  const trialDetail = t => t.status === "completed" ? `${t.accepted} accepted, ${t.rejected} rejected${t.kind === "serial_control" ? ", sent one at a time" : ""}` : INCOMPLETE_TEXT[t.incomplete_reason] || "It didn't finish.";
  return h`${runHead(st)}${step("02", "Load test")}
  ${verdictBlock("INCONCLUSIVE", trialsLine(result), result.verdict_summary, String(result.verdict_summary).includes("isn't a pass") ? "There isn't enough evidence to decide." : "There isn't enough evidence to decide. This isn't a pass.")}
  <section class="panel" id="trials"><div class="ph"><h2>Trials</h2></div>${list(result.trials).map(t => h`<div class="trial-row"><span class="mono">Trial ${t.trial}</span>${trialPill(t)}<span class="detail fg2">${trialDetail(t)}</span></div>`)}</section>
  ${renderDisclosures(result)}
  ${st.replay ? "" : h`<div class="notice info"><b>What to do</b><span class="fg2">Run it again. If it keeps stopping early, the target may be hanging under load.</span></div>`}
  ${backRow()}`;
}

/* ---------- PASS with no fix behind it. It's never "Fix verified". ---------- */
function renderPassState(st) {
  const result = st.result;
  const matched = result.state_verification && result.state_verification.status === "matched";
  const title = matched ? `0 violations in ${result.completeness ? result.completeness.trials_completed : 0} trials.` : "The responses passed, but the database check didn't match.";
  return h`${runHead(st)}${step("02", "Load test")}
  ${verdictBlock("PASS", trialsLine(result), title, matched ? result.verdict_summary : `${databaseEvidence(result)} Don't treat this as a pass.`, matched ? "" : "unverified")}
  ${evidenceStack(result)}${backRow()}`;
}

/* ---------- 9 · hostile build containment ---------- */
function probeResult(p, target) {
  // cpu_spin is throttled, not stopped, so `not_blocked` here means limited. It never reads "blocked".
  if (p.name === "cpu_spin") return { kind: "limited", label: "Limited", note: "not blocked" };
  if (p.name === "guard") return { kind: "none", label: "Attacks not run", note: "" };
  const oom = target && target.oom_killed;
  if (p.result === "no_result" && p.name === "memory" && oom) return { kind: "killed", label: "Killed at limit", note: `${target.memory_limit_mb} MB` };
  if (p.result === "no_result") return { kind: "none", label: "No result", note: "" };
  if (p.result === "inconclusive") return { kind: "none", label: "Inconclusive", note: "" };
  if (p.result !== "blocked") return { kind: "escaped", label: p.name === "secrets_env" ? "Names found" : "Not blocked", note: "" };
  if (p.name === "secrets_env") return { kind: "absent", label: "None found", note: "" };
  if (p.name === "docker_socket") return { kind: "absent", label: "Not present", note: "" };
  if (p.name === "memory" && oom) return { kind: "killed", label: "Killed at limit", note: `${target.memory_limit_mb} MB` };
  if (p.name === "memory") return { kind: "blocked", label: "Blocked", note: target ? `${target.memory_limit_mb} MB limit` : "" };
  if (p.name === "fork") return { kind: "blocked", label: "Blocked", note: target && target.pids_limit != null ? `limit of ${target.pids_limit} processes` : "" };
  return { kind: "blocked", label: "Blocked", note: "" };
}
function probeReported(p) {
  if (p.name === "cpu_spin") return p.note || "CPU use is throttled, not stopped.";
  if (p.name === "secrets_env" && p.result === "blocked") return "It says it found no key names in its environment.";
  if (p.name === "docker_socket" && p.result === "blocked") return "It says the socket doesn't exist.";
  return { blocked: "It says it was blocked.", not_blocked: "It says it wasn't blocked.", inconclusive: "It couldn't tell.", no_result: "It started this and never reported a result." }[p.result] || "It reported nothing usable.";
}
function probeVerified(p, host) {
  const t = host.target;
  if (!t) return "Not available: the host has no facts about this container.";
  switch (p.name) {
    case "egress_external": case "egress_dns": return `Container network: ${t.network}`;
    case "secrets_env": return t.secret_env_names == null ? "Not recorded." : `Environment names, read by the host: ${t.secret_env_names} look like keys`;
    case "docker_socket": return t.docker_socket_mounted == null ? "Not recorded." : t.docker_socket_mounted ? "Docker socket mounted" : "No Docker socket mounted";
    case "write_outside_tmp": return t.read_only_rootfs ? "Root filesystem mounted read-only" : "Root filesystem writable";
    case "privileges": return `${t.all_caps_dropped ? "All capabilities dropped" : "Capabilities kept"}; no-new-privileges ${t.no_new_privileges ? "set" : "not set"}`;
    case "memory": return `Memory limit ${t.memory_limit_mb} MB; out-of-memory flag ${t.oom_killed ? "set" : "not set"}`;
    case "fork": return t.pids_limit == null ? "No process limit" : `Process limit ${t.pids_limit}`;
    case "cpu_spin": return `CPU limit ${t.cpu_limit}${host.wall_limit_s != null ? `; wall-clock limit ${Math.round(num(host.wall_limit_s))} s` : ""}`;
    default: return "Nothing to check.";
  }
}
function renderHostileState(st) {
  const result = st.result, host = result.host_verified || {}, target = host.target;
  const icon = { blocked: I.shield, absent: I.empty, killed: I.stop, limited: I.gauge, none: I.empty, escaped: I.x };
  const rows = list(result.probes).map(p => ({ p, res: probeResult(p, target) }));
  const attacks = rows.filter(r => r.p.name !== "guard");
  const count = k => rows.filter(r => r.res.kind === k).length;
  const contained = attacks.length > 0 && rows.length === attacks.length && attacks.every(r => ["blocked", "absent", "killed", "limited"].includes(r.res.kind)) && target && target.network === "none";
  const open = rows.filter(r => r.res.kind === "escaped" || r.res.kind === "none").length;
  const headline = contained ? `The build tried ${attacks.length} ways to escape. None got out.` : rows.length ? `The build tried ${attacks.length} ways to escape. ${open} weren't contained or have no result.` : "The build reported no attempts.";
  const hostFacts = target ? [
    { k: "Network", v: target.network, ok: target.network === "none" },
    { k: "Filesystem", v: target.read_only_rootfs ? "read-only" : "writable", ok: target.read_only_rootfs },
    { k: "Capabilities", v: target.all_caps_dropped ? "all dropped" : "some kept", ok: target.all_caps_dropped },
    { k: "Memory · processes · CPU", v: `${target.memory_limit_mb} MB · ${target.pids_limit == null ? "no limit" : target.pids_limit} · ${target.cpu_limit}`, ok: target.limits_applied },
    { k: "User", v: target.non_root ? "not root" : "root", ok: target.non_root },
  ] : [];
  const vm = S.status && S.status.vm;
  const left = host.containers_left;
  const verdictText = result.verdict === "PASS" ? "INCONCLUSIVE" : result.verdict || "NO VERDICT";
  return h`${runHead(st)}${step("02", "Containment", "what each attack tried, the result, and how we know")}
  <section class="panel fade-in" id="attacks"><div class="ph"><h2>${headline}</h2>
    <span class="sp counts"><span class="res res-blocked">${I.shield} Blocked ${count("blocked")}</span><span class="res res-absent">${I.empty} Nothing there ${count("absent")}</span><span class="res res-killed">${I.stop} Killed at limit ${count("killed")}</span><span class="res res-limited">${I.gauge} Limited ${count("limited")}</span>${open ? h`<span class="res res-none">${I.x} Not contained or unknown ${open}</span>` : ""}</span></div>
    <div class="attacks">
      <div class="atk atk-h"><span>Attempted</span><span>Result</span><span>Reported by the code itself</span><span>Verified by the host</span></div>
      ${rows.map(({ p, res }) => h`<div class="atk" data-probe="${p.name}"><span><span class="nm">${p.label}</span></span>
        <span><span class="res res-${res.kind}">${icon[res.kind]} ${res.label}</span>${res.note ? h`<span class="res-note">${res.note}</span>` : ""}</span>
        <span class="cell"><span class="eyebrow">Reported by the code itself</span>${probeReported(p)}</span><span class="cell host-v"><span class="eyebrow">Verified by the host</span>${probeVerified(p, host)}</span></div>`)}
    </div></section>
  ${hostFacts.length ? h`<div class="host-title"><span class="eyebrow">Verified by the host</span><span class="muted small">Read from outside the sandbox with docker inspect. The build can't change these.</span></div>
  <section class="host" aria-label="Verified by the host">${hostFacts.map(x => h`<div><span class="k">${x.k}</span><b class="${x.ok ? "" : "no"}">${x.ok ? I.check : I.x}<span>${x.v}</span></b></div>`)}</section>` : ""}
  <section class="endline" id="endline"><div class="v-block INCONCLUSIVE">${verdictText}</div>
    <div class="cell"><b>Verdict: ${verdictText.toLowerCase()}</b><span class="fg2 small">A hostile build doesn't test correctness, so there is no pass or fail. This isn't a pass.</span></div>
    ${vm ? h`<div class="cell"><b class="${vm.healthy ? "ok" : "bad"}">${vm.healthy ? I.check : I.x}<span>${vm.healthy ? "Server healthy" : "Server needs attention"}</span></b><span class="muted small">${st.replay ? "checked now, not at the time of this run" : "from the server's status"}</span></div>` : ""}
    <div class="cell"><b class="${left === 0 ? "ok" : "bad"}">${left === 0 ? I.check : I.x}<span>${left == null ? "Containers left unknown" : plural(num(left), "container") + " left"}</span></b><span class="muted small">counted by the host after the run</span></div></section>
  ${renderDisclosures(result)}
  ${backRow()}`;
}

/* ---------- which view ---------- */
function renderRunView(st) {
  const result = st.result;
  if (!result) return renderRunning(st);
  const hostile = result.target === "hostile-build" || st.target === "hostile-build";
  if (hostile && (list(result.probes).length || result.status === "completed")) return renderHostileState(st);
  if (result.status === "failed" || !result.verdict || result.verdict === "ERROR") return renderFailedRun(st);
  if (result.verdict === "INCONCLUSIVE") return renderInconclusiveState(st);
  if (result.verdict === "PASS") return renderPassState(st);
  return st.fix ? renderFixState(st) : renderFailState(st);
}
function renderTop() {
  const status = S.status;
  const off = status && !status.agent_enabled, busy = status && status.busy;
  const label = !status ? (S.statusFailed ? "Server unreachable" : "Connecting") : busy ? "Sandbox in use" : off ? (String(status.agent_disabled_reason).startsWith("fix_") ? "Fix limit reached" : "Live agent off") : "Live agent on";
  return h`<span class="status" id="server-status"><span class="dot ${!status || off ? "off" : busy ? "busy" : ""}" aria-hidden="true"></span>${label}</span>
    <button class="iconbtn" data-act="theme" aria-label="Toggle light and dark theme" title="Toggle theme">${I.sun}</button>`;
}
function render() {
  let main;
  try {
    main = part(S.view === "run" && S.current ? renderRunView(S.current) : renderStart());
  } catch (error) {
    main = part(h`<div class="notice fail" role="alert"><b>This result couldn't be shown</b><span class="fg2">The server sent something this page doesn't understand.</span></div>`);
  }
  mount("topRight", renderTop());
  const replay = S.view === "run" && S.current && S.current.replay;
  mount("replayBanner", replay ? h`<div class="replay" role="note" id="replay-banner"><div class="replay-in"><b>Replay, not live</b><span class="mono">Recorded ${when(S.current.recorded_at)} · run ${S.current.id}</span><span class="small">Nothing on this page is happening now.</span></div></div>` : "");
  if (main === lastHtml) return;
  lastHtml = main;
  const open = [...document.querySelectorAll("details[data-key][open]")].map(d => d.dataset.key);
  const focused = document.activeElement && document.activeElement.id;
  mount("app", raw(main));
  open.forEach(key => { const d = document.querySelector(`details[data-key="${CSS.escape(key)}"]`); if (d) d.open = true; });
  if (focused && document.getElementById(focused)) document.getElementById(focused).focus({ preventScroll: true });
}
function moved() {
  lastHtml = "";
  render();
  window.scrollTo(0, 0);
  document.getElementById("app").focus({ preventScroll: true });
}

/* ---------- talking to the API ---------- */
async function refreshStatus() {
  const { ok, body } = await api("/api/status");
  if (ok && Array.isArray(body.targets)) { S.status = body; S.statusFailed = false; } else if (!S.status) S.statusFailed = true;
  render();
}
async function refreshReplays() {
  const { ok, body } = await api("/api/replays");
  if (ok && Array.isArray(body.replays)) { S.replays = body.replays; render(); }
}
const terminal = st => ["done", "failed"].includes(st.stage) && !(st.fix && ["queued", "running"].includes(st.fix.status));
async function follow(id) {
  const token = ++pollToken;
  for (;;) {
    const { ok, status, body } = await api(`/api/runs/${encodeURIComponent(id)}`);
    if (token !== pollToken) return;
    if (ok) {
      S.current = body; S.pollNote = null; S.view = "run";
      render();
      if (terminal(body)) { refreshStatus(); refreshReplays(); return; }
    } else if (status === 404) {
      goHome({ title: "That run is gone", message: body.message || "Only recent runs are kept." });
      return;
    } else {
      S.pollNote = "The server isn't answering. This page keeps trying.";
      render();
    }
    await new Promise(resolve => setTimeout(resolve, 1000));
    if (token !== pollToken) return;
  }
}
function goHome(notice) {
  pollToken++;
  S.view = "start"; S.current = null; S.fixNotice = null; S.pollNote = null;
  S.notice = notice || null;
  try { history.replaceState(null, "", "#start"); } catch (e) { /* the address bar is only a convenience */ }
  moved();
  refreshStatus(); refreshReplays();
}
async function startRun() {
  const chosen = document.querySelector('input[name="target"]:checked');
  if (!chosen) return;
  S.target = chosen.value;
  const { ok, status, body, retryAfter } = await api("/api/runs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ target: chosen.value }) });
  if (!ok) {
    S.notice = { title: status === 429 ? "Please wait" : "The run didn't start", message: body.message || "The server refused the run." };
    if (status === 429 && retryAfter > 0) S.cooldownUntil = Date.now() + retryAfter * 1000;
    render();
    return;
  }
  S.notice = null; S.cooldownUntil = 0;
  try { history.replaceState(null, "", `#run/${body.id}`); } catch (e) { /* see goHome */ }
  S.view = "run";
  S.current = { id: body.id, target: chosen.value, stage: "queued", replay: false, events: [], created_at: null, result: null, fix: null };
  moved();
  follow(body.id);
}
async function startFix() {
  const id = S.current && S.current.id;
  if (!id) return;
  const { ok, body } = await api(`/api/runs/${encodeURIComponent(id)}/fix`, { method: "POST" });
  if (!ok) { S.fixNotice = body.message || "The fix didn't start."; refreshStatus(); render(); return; }
  S.fixNotice = null;
  follow(id);
}
async function openReplay(id) {
  pollToken++;
  const { ok, body } = await api(`/api/replays/${encodeURIComponent(id)}`);
  if (!ok) { goHome({ title: "That replay can't be opened", message: body.message || "There's no such replay." }); return; }
  // A replay is forced to `replay: true` here, whatever arrives, so it can't be drawn as live.
  S.current = { ...body, replay: true, stage: "done", events: [], fix_available: false };
  S.view = "run";
  try { history.replaceState(null, "", `#replay/${id}`); } catch (e) { /* see goHome */ }
  moved();
}

/* ---------- events ---------- */
document.addEventListener("click", e => {
  const el = e.target.closest("[data-act]");
  if (!el) return;
  const act = el.dataset.act;
  if (act === "home") { e.preventDefault(); goHome(); }
  else if (act === "run") startRun();
  else if (act === "fix") startFix();
  else if (act === "replay") openReplay(el.dataset.id);
  else if (act === "theme") {
    const root = document.documentElement;
    const dark = root.dataset.theme ? root.dataset.theme === "dark" : !matchMedia("(prefers-color-scheme: light)").matches;
    root.dataset.theme = dark ? "light" : "dark";
    try { localStorage.setItem("ff-theme", root.dataset.theme); } catch (err) { /* only a convenience */ }
  }
});
document.addEventListener("change", e => { if (e.target.name === "target") S.target = e.target.value; });

function boot() {
  try { const saved = localStorage.getItem("ff-theme"); if (saved === "light" || saved === "dark") document.documentElement.dataset.theme = saved; } catch (e) { /* only a convenience */ }
  mount("foot", h`<span>${SITE.footer}</span><a href="${SITE.repo}" target="_blank" rel="noopener">${SITE.repoLabel}</a>`);
  render();
  const [kind, id] = (location.hash || "").slice(1).split("/");
  refreshStatus(); refreshReplays();
  if (kind === "run" && RUN_ID.test(id || "")) follow(id);
  else if (kind === "replay" && RUN_ID.test(id || "")) openReplay(id);
  setInterval(refreshStatus, 5000);
  setInterval(() => {
    if (!S.cooldownUntil) return;
    if (S.cooldownUntil <= Date.now()) { S.cooldownUntil = 0; S.notice = null; }
    render();
  }, 1000);
}
boot();
