// S0 (M0.6 dry run): hit the mock upstream directly. Proves the whole benchmark path
// (k6 -> converter -> job summary -> artifact -> gh-pages chart). Not a budgeted scenario.
import http from 'k6/http';
import { check } from 'k6';
import { TARGET, makeOptions } from '../k6/lib/config.js';

export const options = makeOptions();

export default function () {
  const res = http.get(`${TARGET}/dry-run`);
  check(res, { 'status is 200': (r) => r.status === 200 });
}
