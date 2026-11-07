// A placeholder page. It renders only from the API, and every value becomes a text node:
// nothing here may build HTML from a string.
"use strict";

function el(tag, attributes, ...children) {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(attributes || {})) {
    if (name === "onclick") node.addEventListener("click", value);
    else if (name === "disabled") node.disabled = Boolean(value);
    else node.setAttribute(name, value);
  }
  for (const child of children) {
    if (child === null || child === undefined) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function show(id, ...nodes) {
  document.getElementById(id).replaceChildren(...nodes);
}

function table(headers, rows) {
  return el("table", null,
    el("thead", null, el("tr", null, ...headers.map((name) => el("th", null, name)))),
    el("tbody", null, ...rows.map((row) => el("tr", null, ...row.map((cell) => el("td", null, cell === null ? "–" : String(cell)))))),
  );
}

function pairs(object) {
  return Object.entries(object).map(([name, value]) => `${name}=${value}`).join(", ");
}

async function api(path, options) {
  const response = await fetch(path, options);
  const body = await response.json();
  return { ok: response.ok, status: response.status, body };
}

function notice(text) {
  document.getElementById("notice").textContent = text;
}

// Status

let latestStatus = null;

async function refreshStatus() {
  try {
    const { body } = await api("/api/status");
    latestStatus = body;
    const budget = body.budget;
    show("status",
      el("p", null, `VM ${body.vm.healthy ? "healthy" : "unhealthy"}; containers now: ${body.vm.containers_left ?? "unknown"}; ` +
        `memory free: ${body.vm.memory_available_mb ?? "?"} MB; disk free: ${body.vm.disk_free_mb ?? "?"} MB; load: ${body.vm.load_1m ?? "?"}`),
      el("p", null, `${body.busy ? "Busy" : "Idle"}; queue ${body.queue_length} of ${body.queue_limit}; cooldown ${body.cooldown_s} s`),
      el("p", null, `Agent ${body.agent_enabled ? "on" : `off (${body.agent_disabled_reason})`}; ` +
        `spent today $${budget.daily_usd_spent} of $${budget.daily_usd_cap}, total $${budget.total_usd_spent} of $${budget.total_usd_cap}` +
        `${budget.exhausted ? "; budget used up" : ""}`),
    );
    show("targets", ...body.targets.map((target) =>
      el("button", { onclick: () => startRun(target.id) }, `Run ${target.title}`)),
      ...body.targets.map((target) => el("p", null, `${target.title}: ${target.description}`)),
    );
  } catch {
    show("status", "The server can't be reached.");
  }
}

// Runs

let pollTimer = null;

async function startRun(target) {
  notice("Starting…");
  const { ok, body } = await api("/api/runs", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ target }),
  });
  if (!ok) {
    notice(`Refused (${body.reason}): ${body.message}`);
    return;
  }
  notice("");
  poll(body.id);
}

async function startFix(id) {
  const { ok, body } = await api(`/api/runs/${encodeURIComponent(id)}/fix`, { method: "POST" });
  if (!ok) notice(`Refused (${body.reason}): ${body.message}`);
  else poll(id);
}

async function poll(id) {
  clearTimeout(pollTimer);
  const { ok, body } = await api(`/api/runs/${encodeURIComponent(id)}`);
  if (!ok) {
    show("run", `${body.reason}: ${body.message}`);
    return;
  }
  show("run", renderRunState(body));
  if (!["done", "failed"].includes(body.stage)) pollTimer = setTimeout(() => poll(id), 1000);
  else refreshReplays();
}

function renderRunState(state) {
  return el("div", null,
    el("p", null, `Live run of ${state.target}. Stage: ${state.stage}` +
      `${state.queue_position ? `, position ${state.queue_position} in the queue` : ""}`),
    el("ol", null, ...state.events.map((event) =>
      el("li", null, `${event.at} ${event.name}${event.attempt ? ` ${event.attempt}` : ""}`))),
    state.result ? renderResult(state.result) : null,
    state.fix_available ? el("button", { onclick: () => startFix(state.id) }, "Ask the agent for a fix") : null,
    state.fix ? renderFix(state.fix) : null,
  );
}

function renderContrast(contrast) {
  return el("div", null,
    el("h3", null, `Trial ${contrast.trial}`),
    table(["Accepted", "Rejected", "Unexpected", "Transport errors", "Stock before", "Stock after", "Orders"],
      [[contrast.accepted, contrast.rejected, contrast.unexpected, contrast.transport_error,
        contrast.stock_before, contrast.stock_after, contrast.orders]]),
    el("p", null, "Counts are measured by the runner. Stock and orders are reported by the target."),
  );
}

function renderTimeline(timeline) {
  const end = Math.max(1, ...timeline.points.map((point) => point.offset_ms + point.duration_ms));
  return el("div", null,
    el("h3", null, `Requests in trial ${timeline.trial}${timeline.truncated ? ` (sample of ${timeline.total_requests})` : ""}`),
    table(["Start (ms)", "Duration (ms)", "Outcome"],
      timeline.points.map((point) => [point.offset_ms, point.duration_ms, point.outcome])),
    el("p", null, `Span: ${end.toFixed(1)} ms`),
  );
}

function renderContainer(name, facts) {
  if (!facts) return [name, "no facts", null, null, null, null, null];
  return [name, facts.network, facts.read_only_rootfs, facts.all_caps_dropped, facts.limits_applied, facts.oom_killed,
    `${facts.memory_limit_mb} MB, ${facts.cpu_limit} CPU, ${facts.pids_limit} pids`];
}

function renderHost(host) {
  return el("div", null,
    el("h3", null, "Verified by the host"),
    table(["Container", "Network", "Read-only", "Caps dropped", "Limits applied", "OOM flag", "Limits"],
      [renderContainer("target", host.target), renderContainer("runner", host.runner)]),
    el("p", null, `Containers left: ${host.containers_left ?? "unknown"}; wall ${host.wall_s ?? "?"} s of ${host.wall_limit_s ?? "?"} s` +
      `${host.killed_at_wall_limit ? "; killed at the wall limit" : ""}`),
  );
}

function renderResult(result) {
  return el("div", null,
    el("p", { class: "verdict" }, result.verdict || "No verdict"),
    el("p", null, result.verdict_summary),
    result.failure ? el("p", null, `Failure: ${result.failure.reason}`) : null,
    result.completeness ? el("p", null, `Completeness: ${result.completeness.status}, ` +
      `${result.completeness.trials_completed} of ${result.completeness.trials_planned} trials; serial control ${result.completeness.serial_control}`) : null,
    result.contrast ? renderContrast(result.contrast) : null,
    result.violations.length ? el("div", null, el("h3", null, "Proven violations"),
      table(["Id", "Type", "Invariant", "Trials", "Observed"],
        result.violations.map((item) => [item.id, item.type, item.invariant, item.trials_violated, pairs(item.observed)]))) : null,
    result.suspected_causes.length ? el("div", null, el("h3", null, "Suspected causes (not proven)"),
      el("ul", null, ...result.suspected_causes.map((item) => el("li", null, `${item.cause} (${item.status}): ${item.text}`)))) : null,
    result.trials.length ? el("div", null, el("h3", null, "Trials"),
      table(["Trial", "Kind", "Status", "Reason", "Accepted", "Rejected", "Stock after", "Orders", "Peak in flight"],
        result.trials.map((item) => [item.trial, item.kind, item.status, item.incomplete_reason, item.accepted, item.rejected, item.stock_after, item.orders, item.peak_in_flight]))) : null,
    result.timeline ? renderTimeline(result.timeline) : null,
    result.performance ? el("p", null, `Performance, an observation only: p50 ${result.performance.latency_ms.p50} ms, p95 ${result.performance.latency_ms.p95} ms`) : null,
    result.comparison ? el("div", null, el("h3", null, `Comparison: ${result.comparison.status}; fix verified: ${result.comparison.fix_verified}`),
      el("p", null, `Build ${result.comparison.baseline_build_id} to ${result.comparison.candidate_build_id} (reported by the target)`),
      table(["Invariant", "Result", "Reason"], result.comparison.invariants.map((item) => [item.name, item.result, item.reason]))) : null,
    el("p", null, `Database check: ${result.state_verification.status}${result.state_verification.reason ? ` (${result.state_verification.reason})` : ""}`),
    result.deployed_code_sha256 ? el("p", null, `Deployed code: ${result.deployed_code_sha256}`) : null,
    result.probes ? el("div", null, el("h3", null, "What the target says it tried (self-reported)"),
      table(["Attack", "Result", "Note"], result.probes.map((item) => [item.label, item.result, item.note]))) : null,
    renderHost(result.host_verified),
    el("h3", null, "Disclosures"),
    el("ul", null, ...result.disclosures.map((item) => el("li", null, item))),
  );
}

function renderFix(fix) {
  return el("div", null,
    el("h3", null, `Fix: ${fix.status}, model ${fix.model}, $${fix.total_cost_usd}`),
    fix.stop_text ? el("p", null, fix.stop_text) : null,
    ...fix.attempts.map((attempt) => el("div", null,
      el("h4", null, `Attempt ${attempt.attempt} of ${fix.max_attempts}: ${attempt.passed ? "verified" : "rejected"}`),
      attempt.failure_text ? el("p", null, attempt.failure_text) : null,
      attempt.policy_violations.length ? el("ul", null, ...attempt.policy_violations.map((item) => el("li", null, item))) : null,
      el("p", null, `${attempt.input_tokens}+${attempt.output_tokens} tokens, $${attempt.cost_usd}`),
      attempt.diff ? el("pre", null, attempt.diff) : null,
      attempt.candidate ? renderResult(attempt.candidate) : null,
    )),
  );
}

// Replays

async function refreshReplays() {
  const { body } = await api("/api/replays");
  if (!body.replays.length) {
    show("replays", "None stored yet. They only come from real runs on this server.");
    return;
  }
  show("replays", el("ul", null, ...body.replays.map((item) => el("li", null,
    el("button", { onclick: () => openReplay(item.id) }, "View"),
    el("span", { class: "label" }, "REPLAY"),
    ` ${item.target}: ${item.verdict}${item.fix_verified ? ", fix verified" : ""}, recorded ${item.recorded_at}`,
  ))));
}

async function openReplay(id) {
  const { ok, body } = await api(`/api/replays/${encodeURIComponent(id)}`);
  if (!ok) {
    show("replay", `${body.reason}: ${body.message}`);
    return;
  }
  show("replay",
    el("p", null, el("span", { class: "label" }, "REPLAY"), ` Not live. Recorded ${body.recorded_at}.`),
    renderResult(body.result),
    body.fix ? renderFix(body.fix) : null,
  );
}

refreshStatus();
refreshReplays();
setInterval(refreshStatus, 5000);
