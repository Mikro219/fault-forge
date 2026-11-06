// Sends a fixed number of requests from concurrent VUs and records one ff_req sample per request.
// All parameters arrive as one JSON value in FF_SPEC. See docs/PLAN.md §4.2.
import exec from 'k6/execution';
import http from 'k6/http';
import { Trend } from 'k6/metrics';

const spec = JSON.parse(__ENV.FF_SPEC);
const MAX_CAPTURED_LENGTH = 128;

const ffReq = new Trend('ff_req', true);

export const options = {
  scenarios: {
    burst: {
      executor: 'shared-iterations',
      vus: spec.vus,
      iterations: spec.iterations,
      maxDuration: `${spec.max_duration_s}s`,
      gracefulStop: '0s',
    },
  },
  maxRedirects: 0,
};

http.setResponseCallback(http.expectedStatuses(...spec.accepted_status, ...spec.rejected_status));

// Substitutes ${VU} and ${ITER} inside JSON string values only, never in paths or headers.
function render(value, vu, iter) {
  if (typeof value === 'string') {
    return value.replaceAll('${VU}', String(vu)).replaceAll('${ITER}', String(iter));
  }
  if (Array.isArray(value)) {
    return value.map((item) => render(item, vu, iter));
  }
  if (value !== null && typeof value === 'object') {
    return Object.fromEntries(
      Object.entries(value).map(([key, item]) => [key, render(item, vu, iter)]),
    );
  }
  return value;
}

function classify(response) {
  // Only status 0 means transport failure. k6 also sets error_code on HTTP 4xx and 5xx.
  if (response.status === 0) {
    return 'transport_error';
  }
  if (spec.accepted_status.includes(response.status)) {
    return 'accepted';
  }
  if (spec.rejected_status.includes(response.status)) {
    return 'rejected';
  }
  return 'unexpected';
}

function capture(response, outcome) {
  if (outcome !== 'accepted' || !spec.capture_field) {
    return '';
  }
  try {
    const value = response.json(spec.capture_field);
    return value === undefined || value === null ? '' : String(value).slice(0, MAX_CAPTURED_LENGTH);
  } catch (error) {
    return '';
  }
}

export default function () {
  const vu = exec.vu.idInTest;
  const iter = exec.vu.iterationInScenario;
  const requestId = `t${spec.trial}-${exec.scenario.iterationInTest}`;
  const body = spec.json === null ? null : JSON.stringify(render(spec.json, vu, iter));

  const startedAt = Date.now();
  const response = http.request(spec.method, spec.url, body, {
    headers: { 'Content-Type': 'application/json', 'X-FF-Request-Id': requestId },
    tags: { name: 'ff_target' },
    timeout: `${spec.max_duration_s}s`,
    responseType: spec.capture_field ? 'text' : 'none',
  });
  const endedAt = Date.now();

  const outcome = classify(response);
  ffReq.add(response.timings.duration, {
    ff_outcome: outcome,
    ff_status: String(response.status),
    ff_request_id: requestId,
    ff_vu: String(vu),
    ff_iter: String(iter),
    ff_trial: String(spec.trial),
    ff_captured: capture(response, outcome),
    ff_t_start: String(startedAt),
    ff_t_end: String(endedAt),
  });
}

export function handleSummary(data) {
  return { [spec.summary_path]: JSON.stringify(data) };
}
