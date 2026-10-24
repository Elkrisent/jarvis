// S2 (IMPLEMENTATION C.2): pass-through, unique keys, cache off. Purpose: miss overhead.
// Run twice with the same options: TARGET=<mock upstream> (direct) and TARGET=<gateway>/gw/<slug>
// with KEY=<JARVIS key> (proxied); scripts/bench/overhead.py subtracts the two.
//
// The load is an arrival RATE (requests per second, open model), not a number of looping clients:
// with closed-loop clients a slow gateway slows the clients down and the queueing it causes is
// hidden. Overhead is only meaningful below saturation, so the rate is a parameter (RATE, default
// 100 req/s) and the saturation point is measured separately.
import http from 'k6/http';
import { check } from 'k6';
import { TARGET, DURATION, summaryTrendStats } from '../k6/lib/config.js';

const RATE = Number(__ENV.RATE || 100);

export const options = {
  summaryTrendStats,
  thresholds: { http_req_failed: ['rate<0.01'] },
  scenarios: {
    s2: {
      executor: 'constant-arrival-rate',
      rate: RATE,
      timeUnit: '1s',
      duration: DURATION,
      preAllocatedVUs: Math.max(50, RATE),
      maxVUs: Math.max(500, RATE * 4),
    },
  },
};

const KEY = __ENV.KEY || '';
const params = KEY ? { headers: { 'X-Jarvis-Key': KEY } } : {};

export default function () {
  // A path nobody has asked for before, so nothing can be answered from a cache.
  const res = http.get(`${TARGET}/s2/${__VU}-${__ITER}`, params);
  check(res, { 'status is 200': (r) => r.status === 200 });
}
