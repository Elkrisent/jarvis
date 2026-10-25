// S5 (IMPLEMENTATION C.2): mixed realistic traffic, weights configurable. Phase 2 has no cache, so
// the mix is what the gateway handles today: item reads, searches with a query, writes (POST) and
// a share of requests the upstream answers 404 (never retried, never counted as an outage).
// An answer counts as "unexpected" when its status is not what the mock gives that kind of call.
// Used by S8 (Redis killed and restored under this load).
import http from 'k6/http';
import { Counter } from 'k6/metrics';
import { TARGET, DURATION, summaryTrendStats } from '../k6/lib/config.js';

const RATE = Number(__ENV.RATE || 100);
const W = {
  item: Number(__ENV.W_ITEM || 70),
  search: Number(__ENV.W_SEARCH || 15),
  post: Number(__ENV.W_POST || 10),
  missing: Number(__ENV.W_MISSING || 5),
};
const TOTAL = W.item + W.search + W.post + W.missing;
const unexpected = new Counter('s5_unexpected');
const requests = new Counter('s5_requests');

export const options = {
  summaryTrendStats,
  scenarios: {
    s5: {
      executor: 'constant-arrival-rate',
      rate: RATE,
      timeUnit: '1s',
      duration: DURATION,
      preAllocatedVUs: Math.max(50, RATE),
      maxVUs: Math.max(500, RATE * 4),
    },
  },
};

const params = { headers: { 'X-Jarvis-Key': __ENV.KEY || '', 'Content-Type': 'application/json' } };

export default function () {
  const pick = Math.random() * TOTAL;
  let res;
  let expected = 200;
  if (pick < W.item) {
    res = http.get(`${TARGET}/items/${Math.floor(Math.random() * 500)}`, params);
  } else if (pick < W.item + W.search) {
    res = http.get(`${TARGET}/search?q=term${Math.floor(Math.random() * 50)}`, params);
  } else if (pick < W.item + W.search + W.post) {
    res = http.post(`${TARGET}/orders`, JSON.stringify({ n: __ITER }), params);
  } else {
    res = http.get(`${TARGET}/missing/${__ITER}`, params);
    expected = 404;
  }
  requests.add(1);
  if (res.status !== expected) unexpected.add(1);
}
