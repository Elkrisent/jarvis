window.BENCHMARK_DATA = {
  "lastUpdate": 1792948745116,
  "repoUrl": "https://github.com/Elkrisent/jarvis",
  "entries": {
    "Telemetry overhead": [
      {
        "commit": {
          "author": {
            "email": "144690045+Elkrisent@users.noreply.github.com",
            "name": "Tarun Ragunath",
            "username": "Elkrisent"
          },
          "committer": {
            "email": "noreply@github.com",
            "name": "GitHub",
            "username": "web-flow"
          },
          "distinct": true,
          "id": "c5307e8ab007aa45f1a8e954bc6a63e209e167d6",
          "message": "Merge pull request #27 from Elkrisent/feat/m1.9-telemetry\n\nfeat: telemetry emitter (M1.9)",
          "timestamp": "2026-10-24T16:23:11+05:30",
          "tree_id": "1f5a872b28bfc091a625fe538c84c3f565226fc9",
          "url": "https://github.com/Elkrisent/jarvis/commit/c5307e8ab007aa45f1a8e954bc6a63e209e167d6"
        },
        "date": 1792839359426,
        "tool": "customSmallerIsBetter",
        "benches": [
          {
            "name": "telemetry_overhead_p99",
            "value": 0.164,
            "unit": "ms"
          },
          {
            "name": "telemetry_overhead_p99_redis_down",
            "value": -0.036,
            "unit": "ms"
          }
        ]
      },
      {
        "commit": {
          "author": {
            "email": "144690045+Elkrisent@users.noreply.github.com",
            "name": "Tarun Ragunath",
            "username": "Elkrisent"
          },
          "committer": {
            "email": "noreply@github.com",
            "name": "GitHub",
            "username": "web-flow"
          },
          "distinct": true,
          "id": "5990438f7656c146a2c4b58c33060589ff026084",
          "message": "Merge pull request #28 from Elkrisent/docs/progress-m1.9-complete\n\ndocs: record M1.9 complete in PROGRESS.md",
          "timestamp": "2026-10-24T17:40:20+05:30",
          "tree_id": "b93ff99dce7ccee263a347c55c26a3eef2c73ea4",
          "url": "https://github.com/Elkrisent/jarvis/commit/5990438f7656c146a2c4b58c33060589ff026084"
        },
        "date": 1792844005245,
        "tool": "customSmallerIsBetter",
        "benches": [
          {
            "name": "telemetry_overhead_p99",
            "value": 0.195,
            "unit": "ms"
          },
          {
            "name": "telemetry_overhead_p99_redis_down",
            "value": -0.042,
            "unit": "ms"
          }
        ]
      },
      {
        "commit": {
          "author": {
            "email": "144690045+Elkrisent@users.noreply.github.com",
            "name": "Tarun Ragunath",
            "username": "Elkrisent"
          },
          "committer": {
            "email": "noreply@github.com",
            "name": "GitHub",
            "username": "web-flow"
          },
          "distinct": true,
          "id": "867099a7e413520b6a6c3a3bb7fae25e24316fa9",
          "message": "Merge pull request #29 from Elkrisent/chore/checkpoint-bench-wiring\n\nchore: run propagation and telemetry benchmarks in the checkpoint script",
          "timestamp": "2026-10-24T18:24:34+05:30",
          "tree_id": "8c5b85284be111e9b15317f19bdad9824291bd0d",
          "url": "https://github.com/Elkrisent/jarvis/commit/867099a7e413520b6a6c3a3bb7fae25e24316fa9"
        },
        "date": 1792846673222,
        "tool": "customSmallerIsBetter",
        "benches": [
          {
            "name": "telemetry_overhead_p99",
            "value": 0.245,
            "unit": "ms"
          },
          {
            "name": "telemetry_overhead_p99_redis_down",
            "value": 0.044,
            "unit": "ms"
          }
        ]
      },
      {
        "commit": {
          "author": {
            "email": "144690045+Elkrisent@users.noreply.github.com",
            "name": "Tarun Ragunath",
            "username": "Elkrisent"
          },
          "committer": {
            "email": "noreply@github.com",
            "name": "GitHub",
            "username": "web-flow"
          },
          "distinct": true,
          "id": "1c0941a46439629e288675ffcad93ae1a1d8ca01",
          "message": "Merge pull request #30 from Elkrisent/feat/checkpoint-1\n\nfix+perf: Checkpoint 1 groundwork (SSRF resolver, sniffio/httptools, S2 benchmark, acceptance stories)",
          "timestamp": "2026-10-24T20:49:22+05:30",
          "tree_id": "25e51ac8550360d55b468f3818422e33366746fd",
          "url": "https://github.com/Elkrisent/jarvis/commit/1c0941a46439629e288675ffcad93ae1a1d8ca01"
        },
        "date": 1792855346069,
        "tool": "customSmallerIsBetter",
        "benches": [
          {
            "name": "telemetry_overhead_p99",
            "value": 0.241,
            "unit": "ms"
          },
          {
            "name": "telemetry_overhead_p99_redis_down",
            "value": 0.017,
            "unit": "ms"
          }
        ]
      },
      {
        "commit": {
          "author": {
            "email": "144690045+Elkrisent@users.noreply.github.com",
            "name": "Tarun Ragunath",
            "username": "Elkrisent"
          },
          "committer": {
            "email": "noreply@github.com",
            "name": "GitHub",
            "username": "web-flow"
          },
          "distinct": true,
          "id": "ab4e3a80820fb25691dd49f1929299926cb5e134",
          "message": "Merge pull request #31 from Elkrisent/perf/gc-freeze\n\nperf(gateway): freeze startup objects out of the GC; S2 stall investigation notes",
          "timestamp": "2026-10-24T21:40:25+05:30",
          "tree_id": "0676b8b84c2d1203cb851aec37f81eecba215621",
          "url": "https://github.com/Elkrisent/jarvis/commit/ab4e3a80820fb25691dd49f1929299926cb5e134"
        },
        "date": 1792858392227,
        "tool": "customSmallerIsBetter",
        "benches": [
          {
            "name": "telemetry_overhead_p99",
            "value": 0.214,
            "unit": "ms"
          },
          {
            "name": "telemetry_overhead_p99_redis_down",
            "value": -0.055,
            "unit": "ms"
          }
        ]
      },
      {
        "commit": {
          "author": {
            "email": "144690045+Elkrisent@users.noreply.github.com",
            "name": "Tarun Ragunath",
            "username": "Elkrisent"
          },
          "committer": {
            "email": "noreply@github.com",
            "name": "GitHub",
            "username": "web-flow"
          },
          "distinct": true,
          "id": "ae6cf9de7717d68d0a673dfbd4d37848cbc1c687",
          "message": "Merge pull request #32 from Elkrisent/feat/checkpoint-1-finish\n\nCheckpoint 1: Proxy",
          "timestamp": "2026-10-24T22:17:49+05:30",
          "tree_id": "4aec3602530c8adc2379c1096651d5a1689a4eef",
          "url": "https://github.com/Elkrisent/jarvis/commit/ae6cf9de7717d68d0a673dfbd4d37848cbc1c687"
        },
        "date": 1792860656642,
        "tool": "customSmallerIsBetter",
        "benches": [
          {
            "name": "telemetry_overhead_p99",
            "value": 0.284,
            "unit": "ms"
          },
          {
            "name": "telemetry_overhead_p99_redis_down",
            "value": 0.024,
            "unit": "ms"
          }
        ]
      },
      {
        "commit": {
          "author": {
            "email": "144690045+Elkrisent@users.noreply.github.com",
            "name": "Tarun Ragunath",
            "username": "Elkrisent"
          },
          "committer": {
            "email": "noreply@github.com",
            "name": "GitHub",
            "username": "web-flow"
          },
          "distinct": true,
          "id": "4fccfcfa5507b27ba072982664feb7fe19c76eae",
          "message": "Merge pull request #33 from Elkrisent/docs/progress-checkpoint-1-tagged\n\ndocs: record Checkpoint 1 merged and v0.1.0 tagged",
          "timestamp": "2026-10-24T22:42:13+05:30",
          "tree_id": "e661e14cdeb1ec4762e6b20cade71e007293741f",
          "url": "https://github.com/Elkrisent/jarvis/commit/4fccfcfa5507b27ba072982664feb7fe19c76eae"
        },
        "date": 1792862106323,
        "tool": "customSmallerIsBetter",
        "benches": [
          {
            "name": "telemetry_overhead_p99",
            "value": 0.327,
            "unit": "ms"
          },
          {
            "name": "telemetry_overhead_p99_redis_down",
            "value": 0.026,
            "unit": "ms"
          }
        ]
      },
      {
        "commit": {
          "author": {
            "name": "Tarun Ragunath",
            "username": "Elkrisent",
            "email": "144690045+Elkrisent@users.noreply.github.com"
          },
          "committer": {
            "name": "GitHub",
            "username": "web-flow",
            "email": "noreply@github.com"
          },
          "id": "4fccfcfa5507b27ba072982664feb7fe19c76eae",
          "message": "Merge pull request #33 from Elkrisent/docs/progress-checkpoint-1-tagged\n\ndocs: record Checkpoint 1 merged and v0.1.0 tagged",
          "timestamp": "2026-10-24T17:12:13Z",
          "url": "https://github.com/Elkrisent/jarvis/commit/4fccfcfa5507b27ba072982664feb7fe19c76eae"
        },
        "date": 1792915679338,
        "tool": "customSmallerIsBetter",
        "benches": [
          {
            "name": "telemetry_overhead_p99",
            "value": 0.3,
            "unit": "ms"
          },
          {
            "name": "telemetry_overhead_p99_redis_down",
            "value": -0.01,
            "unit": "ms"
          }
        ]
      },
      {
        "commit": {
          "author": {
            "email": "144690045+Elkrisent@users.noreply.github.com",
            "name": "Tarun Ragunath",
            "username": "Elkrisent"
          },
          "committer": {
            "email": "noreply@github.com",
            "name": "GitHub",
            "username": "web-flow"
          },
          "distinct": true,
          "id": "79d6973f57c44dd5ac4cc2826258f2f10d08aa42",
          "message": "Merge pull request #34 from Elkrisent/fix/revocation-poll-redis-down\n\nfix: poll Postgres every 5 s while the config pub/sub link is down",
          "timestamp": "2026-10-25T15:22:25+05:30",
          "tree_id": "abaf990b5fa977e4345d15d606ca1e1380eaaf86",
          "url": "https://github.com/Elkrisent/jarvis/commit/79d6973f57c44dd5ac4cc2826258f2f10d08aa42"
        },
        "date": 1792922129962,
        "tool": "customSmallerIsBetter",
        "benches": [
          {
            "name": "telemetry_overhead_p99",
            "value": 0.237,
            "unit": "ms"
          },
          {
            "name": "telemetry_overhead_p99_redis_down",
            "value": -0.004,
            "unit": "ms"
          }
        ]
      },
      {
        "commit": {
          "author": {
            "email": "144690045+Elkrisent@users.noreply.github.com",
            "name": "Tarun Ragunath",
            "username": "Elkrisent"
          },
          "committer": {
            "email": "noreply@github.com",
            "name": "GitHub",
            "username": "web-flow"
          },
          "distinct": true,
          "id": "d7b0043748f2be276e84c0d050c41d800459b6a2",
          "message": "Merge pull request #36 from Elkrisent/fix/revocation-poll-redis-down\n\nfeat: per-request deadline and attempt timeouts (M2.1)",
          "timestamp": "2026-10-25T17:22:38+05:30",
          "tree_id": "291eb108fd2ad5646c712724aea27bc119cbc8c7",
          "url": "https://github.com/Elkrisent/jarvis/commit/d7b0043748f2be276e84c0d050c41d800459b6a2"
        },
        "date": 1792929336147,
        "tool": "customSmallerIsBetter",
        "benches": [
          {
            "name": "telemetry_overhead_p99",
            "value": 0.244,
            "unit": "ms"
          },
          {
            "name": "telemetry_overhead_p99_redis_down",
            "value": -0.02,
            "unit": "ms"
          }
        ]
      },
      {
        "commit": {
          "author": {
            "email": "144690045+Elkrisent@users.noreply.github.com",
            "name": "Tarun Ragunath",
            "username": "Elkrisent"
          },
          "committer": {
            "email": "noreply@github.com",
            "name": "GitHub",
            "username": "web-flow"
          },
          "distinct": true,
          "id": "e8f6dda1d36a4a0e588e60ce92e9cdf9716af369",
          "message": "Merge pull request #37 from Elkrisent/feat/m2.2-retry-policy\n\nfeat: retry policy with full-jitter backoff and Retry-After (M2.2)",
          "timestamp": "2026-10-25T19:33:24+05:30",
          "tree_id": "08d7815b7df0093e0fdb8fefdb439a8f59db5df5",
          "url": "https://github.com/Elkrisent/jarvis/commit/e8f6dda1d36a4a0e588e60ce92e9cdf9716af369"
        },
        "date": 1792937185445,
        "tool": "customSmallerIsBetter",
        "benches": [
          {
            "name": "telemetry_overhead_p99",
            "value": 0.277,
            "unit": "ms"
          },
          {
            "name": "telemetry_overhead_p99_redis_down",
            "value": 0.017,
            "unit": "ms"
          }
        ]
      },
      {
        "commit": {
          "author": {
            "email": "144690045+Elkrisent@users.noreply.github.com",
            "name": "Tarun Ragunath",
            "username": "Elkrisent"
          },
          "committer": {
            "email": "noreply@github.com",
            "name": "GitHub",
            "username": "web-flow"
          },
          "distinct": true,
          "id": "fb34af96fe3a048448a9a859c018679175aa6751",
          "message": "Merge pull request #38 from Elkrisent/feat/m2.3-retry-budget\n\nfeat: per-API retry budget in Redis with local fallback (M2.3)",
          "timestamp": "2026-10-25T19:57:44+05:30",
          "tree_id": "137eed30e7c2a48a9a1f43017bd7ec08d89f4c7f",
          "url": "https://github.com/Elkrisent/jarvis/commit/fb34af96fe3a048448a9a859c018679175aa6751"
        },
        "date": 1792938640524,
        "tool": "customSmallerIsBetter",
        "benches": [
          {
            "name": "telemetry_overhead_p99",
            "value": 0.304,
            "unit": "ms"
          },
          {
            "name": "telemetry_overhead_p99_redis_down",
            "value": -0.008,
            "unit": "ms"
          }
        ]
      },
      {
        "commit": {
          "author": {
            "email": "144690045+Elkrisent@users.noreply.github.com",
            "name": "Tarun Ragunath",
            "username": "Elkrisent"
          },
          "committer": {
            "email": "noreply@github.com",
            "name": "GitHub",
            "username": "web-flow"
          },
          "distinct": true,
          "id": "42532a71abf6edd5e70b3ce96695b2ca06a1ead8",
          "message": "Merge pull request #39 from Elkrisent/feat/m2.4-circuit-breaker\n\nfeat: circuit breaker with shared Redis state, local mirror and singl…",
          "timestamp": "2026-10-25T20:39:41+05:30",
          "tree_id": "dfeccb558dd92a1d4b905d5fae74d8ec658c8100",
          "url": "https://github.com/Elkrisent/jarvis/commit/42532a71abf6edd5e70b3ce96695b2ca06a1ead8"
        },
        "date": 1792941155908,
        "tool": "customSmallerIsBetter",
        "benches": [
          {
            "name": "telemetry_overhead_p99",
            "value": 0.189,
            "unit": "ms"
          },
          {
            "name": "telemetry_overhead_p99_redis_down",
            "value": 0.016,
            "unit": "ms"
          }
        ]
      },
      {
        "commit": {
          "author": {
            "email": "144690045+Elkrisent@users.noreply.github.com",
            "name": "Tarun Ragunath",
            "username": "Elkrisent"
          },
          "committer": {
            "email": "noreply@github.com",
            "name": "GitHub",
            "username": "web-flow"
          },
          "distinct": true,
          "id": "955fa8818953165ff96591f989c28b6a6c967be1",
          "message": "Merge pull request #40 from Elkrisent/feat/m2.5-rate-limiters\n\nfeat: Redis token-bucket rate limiters, inbound and upstream quota (M2.5)",
          "timestamp": "2026-10-25T21:07:25+05:30",
          "tree_id": "5f1b2e26f8e9b821e2e27b34643865069a85b580",
          "url": "https://github.com/Elkrisent/jarvis/commit/955fa8818953165ff96591f989c28b6a6c967be1"
        },
        "date": 1792942822486,
        "tool": "customSmallerIsBetter",
        "benches": [
          {
            "name": "telemetry_overhead_p99",
            "value": 0.274,
            "unit": "ms"
          },
          {
            "name": "telemetry_overhead_p99_redis_down",
            "value": -0.016,
            "unit": "ms"
          }
        ]
      },
      {
        "commit": {
          "author": {
            "email": "144690045+Elkrisent@users.noreply.github.com",
            "name": "Tarun Ragunath",
            "username": "Elkrisent"
          },
          "committer": {
            "email": "noreply@github.com",
            "name": "GitHub",
            "username": "web-flow"
          },
          "distinct": true,
          "id": "156198a7f514589e35997cfd69c81d8e103ecc4c",
          "message": "Merge pull request #41 from Elkrisent/feat/m2.6-fallback\n\nfeat: fallback API with shared deadline, own circuit and credential (M2.6)",
          "timestamp": "2026-10-25T21:39:05+05:30",
          "tree_id": "5e5fa8a8beddc085498e4be90b8f86201b764233",
          "url": "https://github.com/Elkrisent/jarvis/commit/156198a7f514589e35997cfd69c81d8e103ecc4c"
        },
        "date": 1792944725142,
        "tool": "customSmallerIsBetter",
        "benches": [
          {
            "name": "telemetry_overhead_p99",
            "value": 0.28,
            "unit": "ms"
          },
          {
            "name": "telemetry_overhead_p99_redis_down",
            "value": 0.032,
            "unit": "ms"
          }
        ]
      },
      {
        "commit": {
          "author": {
            "email": "144690045+Elkrisent@users.noreply.github.com",
            "name": "Tarun Ragunath",
            "username": "Elkrisent"
          },
          "committer": {
            "email": "noreply@github.com",
            "name": "GitHub",
            "username": "web-flow"
          },
          "distinct": true,
          "id": "c0f19a8ba3a7495da8afd017c7a53d22736bd194",
          "message": "Merge pull request #42 from Elkrisent/feat/m2.7-redis-fail-open\n\nfeat: Redis-down fail-open mode with a health breaker (M2.7)",
          "timestamp": "2026-10-25T22:10:47+05:30",
          "tree_id": "154cc2dd28f6c0faaf91390bd017c05c220459ae",
          "url": "https://github.com/Elkrisent/jarvis/commit/c0f19a8ba3a7495da8afd017c7a53d22736bd194"
        },
        "date": 1792946621645,
        "tool": "customSmallerIsBetter",
        "benches": [
          {
            "name": "telemetry_overhead_p99",
            "value": 0.222,
            "unit": "ms"
          },
          {
            "name": "telemetry_overhead_p99_redis_down",
            "value": -0.038,
            "unit": "ms"
          }
        ]
      },
      {
        "commit": {
          "author": {
            "email": "144690045+Elkrisent@users.noreply.github.com",
            "name": "Tarun Ragunath",
            "username": "Elkrisent"
          },
          "committer": {
            "email": "noreply@github.com",
            "name": "GitHub",
            "username": "web-flow"
          },
          "distinct": true,
          "id": "0b7cd7b5a60ecef7303073e7efe981249c1c96d8",
          "message": "Merge pull request #43 from Elkrisent/fix/chaos-telemetry-events\n\nPhase 2 on Docker: telemetry stream test fix, S4 and S8 scenarios",
          "timestamp": "2026-10-25T22:46:21+05:30",
          "tree_id": "37640637dfef9d63b37ce6950b8b842c49f98130",
          "url": "https://github.com/Elkrisent/jarvis/commit/0b7cd7b5a60ecef7303073e7efe981249c1c96d8"
        },
        "date": 1792948744510,
        "tool": "customSmallerIsBetter",
        "benches": [
          {
            "name": "telemetry_overhead_p99",
            "value": 0.215,
            "unit": "ms"
          },
          {
            "name": "telemetry_overhead_p99_redis_down",
            "value": -0.027,
            "unit": "ms"
          }
        ]
      }
    ]
  }
}