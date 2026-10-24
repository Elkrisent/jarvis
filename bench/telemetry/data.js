window.BENCHMARK_DATA = {
  "lastUpdate": 1792860657526,
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
      }
    ]
  }
}