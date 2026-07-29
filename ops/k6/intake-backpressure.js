// TITAN-OPS-020 intake backpressure load test (Vol 9 §9).
//
// Drives the intake endpoint at ~5x worker capacity and asserts the durability
// contract: intake NEVER returns a 5xx (over-quota is a retryable 429, read-only
// is a 503 — both 4xx/503-that-a-client-retries, not 5xx and not a silent drop),
// and no accepted task is lost — every 201 has a row in the Postgres `tasks`
// ledger, and the queue fully drains once load stops.
//
// This is a CI/staging artifact: it needs the full Compose stack (api + postgres
// + redis + worker) and the k6 binary, so it does NOT run in the per-commit unit
// suite (which covers the same invariants deterministically — admission 429,
// read_only 503, fail-open no-5xx, ledger-before-enqueue, requeue-from-ledger).
//
// Run (staging):
//   K6_BASE_URL=https://staging.titan.internal \
//   K6_TOKEN="$(titanctl mint-token --role owner)" \
//   WORKER_CAPACITY=25 \
//   k6 run ops/k6/intake-backpressure.js
//
// Acceptance (TITAN-OPS-020): the run is GREEN only if `intake_5xx` stays 0 for
// the whole run; the "zero lost tasks + queue drains" half is verified by the
// post-run reconciliation query the runbook pairs with this (accepted task ids
// vs `tasks` rows, and `titan_queue_oldest_pending_seconds` returning to ~0).

import http from 'k6/http'
import { check } from 'k6'
import { Counter } from 'k6/metrics'

const BASE = __ENV.K6_BASE_URL || 'http://localhost:8000'
const TOKEN = __ENV.K6_TOKEN || ''
const WORKER_CAPACITY = parseInt(__ENV.WORKER_CAPACITY || '25', 10)

// Any 5xx on an intake endpoint fails the acceptance criterion outright.
const intake5xx = new Counter('intake_5xx')
const intakeAccepted = new Counter('intake_accepted')
const intakeThrottled = new Counter('intake_throttled') // 429/503 — backpressure, OK

const HEADERS = { 'Content-Type': 'application/json', Authorization: `Bearer ${TOKEN}` }

export const options = {
  scenarios: {
    // ~5x worker capacity of sustained intake for a short window.
    flood: {
      executor: 'constant-arrival-rate',
      rate: WORKER_CAPACITY * 5,
      timeUnit: '1s',
      duration: __ENV.K6_DURATION || '2m',
      preAllocatedVUs: WORKER_CAPACITY * 5,
      maxVUs: WORKER_CAPACITY * 10,
    },
  },
  thresholds: {
    // The load-test's whole point: not a single 5xx on intake.
    intake_5xx: ['count==0'],
  },
}

export function setup() {
  if (!TOKEN) throw new Error('K6_TOKEN is required (a Bearer token for an owner/admin)')
  const name = `k6-intake-${Date.now()}`
  const res = http.post(
    `${BASE}/api/v1/projects`,
    JSON.stringify({ name, display_name: name, brief: 'k6 backpressure load test' }),
    { headers: HEADERS }
  )
  check(res, { 'project created': (r) => r.status === 201 })
  return { projectId: res.json('id') }
}

export default function (data) {
  const res = http.post(
    `${BASE}/api/v1/projects/${data.projectId}/tasks`,
    JSON.stringify({ title: 'k6 task', spec: 'load' }),
    { headers: HEADERS }
  )
  if (res.status >= 500) {
    intake5xx.add(1)
  } else if (res.status === 429 || res.status === 503) {
    intakeThrottled.add(1) // expected backpressure — the client retries, nothing lost
  } else if (res.status === 201) {
    intakeAccepted.add(1)
  }
  check(res, { 'intake never 5xx': (r) => r.status < 500 })
}
