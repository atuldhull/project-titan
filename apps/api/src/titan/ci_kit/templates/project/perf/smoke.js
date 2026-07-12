// k6 performance slot (TITAN-WFL-030, Vol 6 §6.5).
// Endpoints with declared budgets get a k6 script here asserting their p95.
// Run: k6 run perf/smoke.js  (BASE_URL defaults to the local app).
//
// This is a slot, not a load test — replace the threshold and scenario with the
// real per-endpoint p95 budgets from the ArchitecturePackage.
import http from 'k6/http';
import { check } from 'k6';

const BASE_URL = __ENV.BASE_URL || 'http://localhost:8000';

export const options = {
  vus: 5,
  duration: '10s',
  thresholds: {
    // p95 budget: 95% of requests under 250ms. Tighten per endpoint.
    http_req_duration: ['p(95)<250'],
  },
};

export default function () {
  const res = http.get(`${BASE_URL}/widgets/1`);
  check(res, { 'status is 200': (r) => r.status === 200 });
}
