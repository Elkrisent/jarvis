// Shared k6 configuration. Override with -e TARGET=... -e DURATION=... -e VUS=...
export const TARGET = __ENV.TARGET || 'http://localhost:19000';
export const DURATION = __ENV.DURATION || '10s';
export const VUS = Number(__ENV.VUS || 10);

// The trend stats the converters read (scripts/bench/k6_to_gab.py).
export const summaryTrendStats = ['avg', 'min', 'med', 'max', 'p(90)', 'p(95)', 'p(99)'];

export function makeOptions(extra = {}) {
  return {
    vus: VUS,
    duration: DURATION,
    summaryTrendStats,
    thresholds: { http_req_failed: ['rate<0.01'] },
    ...extra,
  };
}
