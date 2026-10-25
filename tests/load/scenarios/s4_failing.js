// S4 (IMPLEMENTATION C.2): failing upstream (50% 503). Purpose: retry budget + circuit fail-fast.
// Through the gateway with KEY=<JARVIS key>; the mock upstream is set to answer 503 half the time.
// Counters tell the outcomes apart: an answer the upstream gave, a JARVIS fail-fast refusal
// (circuit open), or anything else. The runner compares the client request count with the number
// of calls the upstream actually received.
import http from 'k6/http';
import { Counter } from 'k6/metrics';
import { TARGET, DURATION, summaryTrendStats } from '../k6/lib/config.js';

const RATE = Number(__ENV.RATE || 100);
const upstreamAnswers = new Counter('s4_upstream_answers');
const circuitOpen = new Counter('s4_circuit_open');
const other = new Counter('s4_other');

export const options = {
  summaryTrendStats,
  scenarios: {
    s4: {
      executor: 'constant-arrival-rate',
      rate: RATE,
      timeUnit: '1s',
      duration: DURATION,
      preAllocatedVUs: Math.max(50, RATE),
      maxVUs: Math.max(500, RATE * 4),
    },
  },
};

const params = { headers: { 'X-Jarvis-Key': __ENV.KEY || '' } };

export default function () {
  const res = http.get(`${TARGET}/s4/${__VU}-${__ITER}`, params);
  const source = res.headers['X-Jarvis-Source'];
  if (source === 'upstream') {
    upstreamAnswers.add(1);
  } else if (res.status === 503 && res.body && res.body.indexOf('JV_CIRCUIT_OPEN') !== -1) {
    circuitOpen.add(1);
  } else {
    other.add(1);
  }
}
