"""Benchmark scripts: converter, overhead, budget checker, env, docker stats, checkpoint."""

import json
from pathlib import Path

import benchlib
import check_budgets
import checkpoint
import docker_stats
import k6_to_gab
import median_runs
import micro_results
import overhead
import pytest
import write_env

pytestmark = pytest.mark.unit

BUDGETS_YAML = """\
metrics:
  gw_overhead_hit_p50: {unit: ms, direction: lower, budget: 2, spec: x}
  gw_throughput_hit_rps: {unit: req/s, direction: higher, budget: 2000, spec: x}
  token_bucket_take: {unit: us/op, direction: lower, budget: null, spec: x}
"""


def k6_summary(med: float, p95: float, p99: float, rate: float) -> dict[str, object]:
    return {
        "metrics": {
            "http_req_duration": {
                "avg": med,
                "min": 1,
                "med": med,
                "max": 99,
                "p(90)": p95,
                "p(95)": p95,
                "p(99)": p99,
            },
            "http_reqs": {"count": 1000, "rate": rate},
        }
    }


@pytest.fixture
def budgets_file(tmp_path: Path) -> Path:
    path = tmp_path / "budgets.yaml"
    path.write_text(BUDGETS_YAML)
    return path


# ------------------------------------------------------------------------ budgets.yaml


def test_real_budgets_file_loads_and_matches_spec_13() -> None:
    budgets = benchlib.load_budgets(
        Path(__file__).resolve().parents[2] / "benchmarks" / "budgets.yaml"
    )
    expected = {  # SPEC §13
        "gw_overhead_hit_p50": ("lower", 2),
        "gw_overhead_hit_p99": ("lower", 10),
        "gw_overhead_miss_p50": ("lower", 3),
        "gw_overhead_miss_p99": ("lower", 15),
        "gw_throughput_hit_rps": ("higher", 2000),
        "gw_redis_roundtrips_pre": ("lower", 2),
        "gw_redis_roundtrips_post": ("lower", 1),
        "config_propagation_p99": ("lower", 1000),
        "dash_api_p95_overview": ("lower", 200),
        "dash_api_p95_api_detail": ("lower", 200),
    }
    for name, (direction, value) in expected.items():
        assert (budgets[name].direction, budgets[name].budget) == (direction, value), name


def test_every_metric_in_the_guide_c1_has_an_entry() -> None:
    budgets = benchlib.load_budgets(
        Path(__file__).resolve().parents[2] / "benchmarks" / "budgets.yaml"
    )
    c1 = [
        "token_bucket_take",
        "cache_key_build",
        "route_template_match",
        "backoff_compute",
        "redaction_apply",
        "evidence_bundle_build",
        "gw_overhead_hit_p50",
        "gw_overhead_hit_p99",
        "gw_overhead_miss_p50",
        "gw_overhead_miss_p99",
        "gw_throughput_hit_rps",
        "gw_redis_roundtrips_pre",
        "gw_redis_roundtrips_post",
        "dedup_upstream_calls_per_1000_identical",
        "upstream_calls_avoided_ratio",
        "config_propagation_p99",
        "circuit_open_fail_fast_p99",
        "gw_rss_slope_mb_per_hour",
        "worker_rss_slope_mb_per_hour",
        "stack_idle_rss_total_mb",
        "ingest_lag_p99",
        "telemetry_dropped_ratio",
        "dash_api_p95_overview",
        "dash_api_p95_api_detail",
        "web_lcp",
        "web_tti",
        "web_bundle_js_kb",
        "web_perf_score",
        "web_a11y_score",
    ]
    assert [m for m in c1 if m not in budgets] == []


def test_bad_direction_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "b.yaml"
    path.write_text("metrics:\n  x: {unit: ms, direction: sideways, budget: 1}\n")
    with pytest.raises(ValueError, match="direction"):
        benchlib.load_budgets(path)


@pytest.mark.parametrize(
    ("value", "status"),
    [(1.9, "ok"), (2.0, "ok"), (2.1, "over budget")],
)
def test_judge_lower_is_better(value: float, status: str, budgets_file: Path) -> None:
    assert (
        benchlib.judge(value, benchlib.load_budgets(budgets_file)["gw_overhead_hit_p50"]) == status
    )


@pytest.mark.parametrize(("value", "status"), [(2500, "ok"), (2000, "ok"), (1999, "over budget")])
def test_judge_higher_is_better(value: float, status: str, budgets_file: Path) -> None:
    assert (
        benchlib.judge(value, benchlib.load_budgets(budgets_file)["gw_throughput_hit_rps"])
        == status
    )


def test_judge_without_budget_or_unknown(budgets_file: Path) -> None:
    budgets = benchlib.load_budgets(budgets_file)
    assert benchlib.judge(5, budgets["token_bucket_take"]) == "no budget"
    assert benchlib.judge(5, None) == "unknown metric"


# --------------------------------------------------------------------- check_budgets


def _write(path: Path, data: object) -> Path:
    path.write_text(json.dumps(data))
    return path


def test_check_budgets_reports_but_does_not_fail_without_enforce(
    tmp_path: Path, budgets_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    results = _write(tmp_path / "r.json", {"gw_overhead_hit_p50": 5.0})
    assert check_budgets.main([str(results), "--budgets", str(budgets_file)]) == 0
    captured = capsys.readouterr()
    assert "over budget" in captured.out
    assert "not enforced" in captured.err


def test_check_budgets_fails_with_enforce(tmp_path: Path, budgets_file: Path) -> None:
    results = _write(tmp_path / "r.json", {"gw_overhead_hit_p50": 5.0})
    assert check_budgets.main([str(results), "--budgets", str(budgets_file), "--enforce"]) == 1


def test_check_budgets_passes_when_within_budget(tmp_path: Path, budgets_file: Path) -> None:
    results = _write(
        tmp_path / "r.json", {"gw_overhead_hit_p50": 1.0, "gw_throughput_hit_rps": 3000}
    )
    assert check_budgets.main([str(results), "--budgets", str(budgets_file), "--enforce"]) == 0


def test_check_budgets_rejects_a_metric_missing_from_budgets_yaml(
    tmp_path: Path, budgets_file: Path
) -> None:
    results = _write(tmp_path / "r.json", {"typo_metric": 1.0})
    assert check_budgets.main([str(results), "--budgets", str(budgets_file)]) == 1


def test_check_budgets_change_column_and_step_summary(
    tmp_path: Path, budgets_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    results = _write(tmp_path / "r.json", {"gw_overhead_hit_p50": 1.5})
    baseline = _write(tmp_path / "b.json", {"gw_overhead_hit_p50": 1.0})
    check_budgets.main([str(results), "--budgets", str(budgets_file), "--baseline", str(baseline)])
    text = summary.read_text()
    assert "### Benchmarks" in text
    assert "+50.0%" in text
    assert "≤ 2" in text


# ------------------------------------------------------------ k6 conversion and overhead


def test_k6_to_gab_formats_and_names(tmp_path: Path) -> None:
    summary = _write(tmp_path / "s.json", k6_summary(5.0, 8.0, 13.0, 1800.0))
    smaller, bigger, results = tmp_path / "s.out.json", tmp_path / "b.out.json", tmp_path / "r.json"
    k6_to_gab.main(
        [
            str(summary),
            "--prefix",
            "mock_direct",
            "--smaller",
            str(smaller),
            "--bigger",
            str(bigger),
            "--results",
            str(results),
        ]
    )
    assert json.loads(smaller.read_text()) == [
        {"name": "mock_direct_p50", "unit": "ms", "value": 5.0},
        {"name": "mock_direct_p95", "unit": "ms", "value": 8.0},
        {"name": "mock_direct_p99", "unit": "ms", "value": 13.0},
    ]
    assert json.loads(bigger.read_text()) == [
        {"name": "mock_direct_rps", "unit": "req/s", "value": 1800.0}
    ]
    assert json.loads(results.read_text())["mock_direct_p99"] == 13.0


def test_k6_to_gab_extends_files_and_replaces_same_names(tmp_path: Path) -> None:
    smaller, bigger = tmp_path / "s.json", tmp_path / "b.json"
    for prefix, med in (("a", 1.0), ("b", 2.0), ("a", 3.0)):
        summary = _write(tmp_path / "in.json", k6_summary(med, med, med, 10.0))
        k6_to_gab.main(
            [str(summary), "--prefix", prefix, "--smaller", str(smaller), "--bigger", str(bigger)]
        )
    items = {i["name"]: i["value"] for i in json.loads(smaller.read_text())}
    assert items["a_p50"] == 3.0
    assert items["b_p50"] == 2.0
    assert len(json.loads(smaller.read_text())) == 6


def test_overhead_is_proxied_minus_direct_per_percentile() -> None:
    direct = k6_summary(50.0, 52.0, 55.0, 100.0)
    proxied = k6_summary(52.5, 56.0, 62.0, 90.0)
    assert overhead.overhead(direct, proxied, "gw_overhead_hit") == {
        "gw_overhead_hit_p50": 2.5,
        "gw_overhead_hit_p99": 7.0,
    }


def test_overhead_cli_records_overhead_and_throughput(tmp_path: Path) -> None:
    direct = _write(tmp_path / "d.json", k6_summary(50.0, 52.0, 55.0, 100.0))
    proxied = _write(tmp_path / "p.json", k6_summary(51.0, 53.0, 57.0, 2100.0))
    results = tmp_path / "r.json"
    overhead.main(
        [
            str(direct),
            str(proxied),
            "--prefix",
            "gw_overhead_hit",
            "--results",
            str(results),
            "--rps-metric",
            "gw_throughput_hit_rps",
        ]
    )
    assert json.loads(results.read_text()) == {
        "gw_overhead_hit_p50": 1.0,
        "gw_overhead_hit_p99": 2.0,
        "gw_throughput_hit_rps": 2100.0,
    }


def test_micro_results_converts_seconds_to_microseconds_and_strips_prefix(tmp_path: Path) -> None:
    raw = _write(
        tmp_path / "m.json",
        {"benchmarks": [{"name": "test_bench_token_bucket_take", "stats": {"mean": 2e-6}}]},
    )
    results = tmp_path / "r.json"
    micro_results.main([str(raw), "--results", str(results)])
    assert json.loads(results.read_text())["token_bucket_take"] == pytest.approx(2.0)


# ------------------------------------------------------------------------- env.json


def test_cpu_model_and_ram_parsing() -> None:
    assert (
        write_env.cpu_model("processor: 0\nmodel name\t: Fancy CPU @ 3GHz\n") == "Fancy CPU @ 3GHz"
    )
    assert write_env.ram_mb("MemTotal:        8192000 kB\nMemFree: 1 kB\n") == 8000


def test_env_json_contents_and_runner_detection() -> None:
    hosted = write_env.collect(
        "default",
        {"vus": "10"},
        {
            "GITHUB_ACTIONS": "true",
            "RUNNER_ENVIRONMENT": "github-hosted",
            "GITHUB_SHA": "abc",
            "GITHUB_REF_NAME": "main",
        },
    )
    assert (hosted["runner"], hosted["commit"], hosted["branch"]) == (
        "github-hosted",
        "abc",
        "main",
    )
    assert hosted["scenario_params"] == {"vus": "10"}
    assert hosted["jarvis_profile"] == "default"
    for key in ("date", "cpu_model", "cpu_cores", "ram_mb", "os", "python", "node"):
        assert key in hosted
    assert write_env.collect("default", {}, {})["runner"] == "local"


def test_write_env_cli_writes_the_file(tmp_path: Path) -> None:
    out = tmp_path / "env.json"
    write_env.main(["--out", str(out), "--param", "a=b=c", "--profile", "p"])
    data = json.loads(out.read_text())
    assert data["scenario_params"] == {"a": "b=c"}
    assert data["jarvis_profile"] == "p"


# ------------------------------------------------------------------------ docker stats


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1KiB", 1024),
        ("46.5MiB", 46.5 * 1024**2),
        ("2GiB", 2 * 1024**3),
        ("10MB", 10_000_000),
        ("512B", 512),
    ],
)
def test_parse_bytes(text: str, expected: float) -> None:
    assert docker_stats.parse_bytes(text) == pytest.approx(expected)


@pytest.mark.parametrize("bad", ["", "12", "MiB", "3 parsecs"])
def test_parse_bytes_rejects_garbage(bad: str) -> None:
    with pytest.raises(ValueError, match="parse|unit"):
        docker_stats.parse_bytes(bad)


def test_parse_stats_line() -> None:
    line = json.dumps({"Name": "jarvis-test-api-1", "MemUsage": "64MiB / 7.6GiB"})
    assert docker_stats.parse_stats_line(line) == ("jarvis-test-api-1", pytest.approx(64.0))


def test_slope_per_hour() -> None:
    points = [(0.0, 100.0), (1800.0, 105.0), (3600.0, 110.0)]
    assert docker_stats.slope_per_hour(points) == pytest.approx(10.0)
    assert docker_stats.slope_per_hour([(0.0, 5.0)]) == 0.0
    assert docker_stats.slope_per_hour([(1.0, 5.0), (1.0, 9.0)]) == 0.0


def test_summarize_with_match_and_exclude() -> None:
    samples = [
        {
            "ts": 0,
            "containers": {
                "jarvis-test-gateway-1": 50.0,
                "jarvis-test-mock-upstream-1": 20.0,
                "other": 999.0,
            },
        },
        {
            "ts": 3600,
            "containers": {
                "jarvis-test-gateway-1": 60.0,
                "jarvis-test-mock-upstream-1": 20.0,
                "other": 999.0,
            },
        },
    ]
    out = docker_stats.summarize(samples, match="jarvis-test-", exclude="mock-upstream")
    assert out["mean_total_mb"] == pytest.approx(55.0)
    assert out["slope_mb_per_hour"] == pytest.approx(10.0)
    with pytest.raises(ValueError, match="no samples"):
        docker_stats.summarize([])


def test_summarize_cli_writes_requested_metrics(tmp_path: Path) -> None:
    stats = tmp_path / "s.jsonl"
    stats.write_text(
        "\n".join(
            json.dumps({"ts": t, "containers": {"gw": m}}) for t, m in ((0, 10.0), (3600, 14.0))
        )
        + "\n"
    )
    results = tmp_path / "r.json"
    docker_stats.main(
        [
            "summarize",
            str(stats),
            "--results",
            str(results),
            "--total-metric",
            "stack_idle_rss_total_mb",
            "--slope-metric",
            "gw_rss_slope_mb_per_hour",
        ]
    )
    assert json.loads(results.read_text()) == {
        "stack_idle_rss_total_mb": 12.0,
        "gw_rss_slope_mb_per_hour": pytest.approx(4.0),
    }


# ---------------------------------------------------------------------------- checkpoint


def _repo(tmp_path: Path) -> Path:
    (tmp_path / "benchmarks" / "checkpoints").mkdir(parents=True)
    (tmp_path / "benchmarks" / "budgets.yaml").write_text(BUDGETS_YAML)
    return tmp_path


def test_checkpoint_writes_snapshot_and_benchmarks_md(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    results = _write(tmp_path / "r.json", {"gw_overhead_hit_p50": 1.5, "token_bucket_take": 1.2})
    env = _write(tmp_path / "env.json", {"runner": "local"})
    raw = _write(tmp_path / "raw.json", {"hello": 1})
    assert (
        checkpoint.main(
            ["--phase", "0", "--results", str(results), "--env", str(env), "--raw", f"micro={raw}"],
            root=root,
        )
        == 0
    )

    snap = json.loads((root / "benchmarks" / "checkpoints" / "P0.json").read_text())
    assert snap["phase"] == 0
    assert snap["env"] == {"runner": "local"}
    assert snap["results"]["token_bucket_take"] == 1.2
    assert snap["raw"] == {"micro": {"hello": 1}}

    md = (root / "BENCHMARKS.md").read_text().splitlines()
    header = next(line for line in md if line.startswith("| Metric"))
    assert header == "| Metric | Budget | P0 | P1 | P2 | P3 | P4 | P5 | P6 | P7 | Status |"
    row = next(line for line in md if line.startswith("| gw_overhead_hit_p50"))
    assert row.split("|")[2].strip() == "≤ 2 ms"
    assert row.split("|")[3].strip() == "1.5"
    assert row.split("|")[-2].strip() == "ok"


def test_benchmarks_md_uses_latest_checkpoint_for_status_and_shows_each_phase(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    env = _write(tmp_path / "env.json", {})
    for phase, value in ((0, 1.0), (1, 9.0)):
        results = _write(tmp_path / "r.json", {"gw_overhead_hit_p50": value})
        checkpoint.main(
            ["--phase", str(phase), "--results", str(results), "--env", str(env)], root=root
        )
    row = next(
        line
        for line in (root / "BENCHMARKS.md").read_text().splitlines()
        if line.startswith("| gw_overhead_hit_p50")
    )
    cells = [c.strip() for c in row.split("|")[1:-1]]
    assert cells[2:4] == ["1", "9"]
    assert cells[-1] == "over budget"


def test_benchmarks_md_lists_budgeted_metrics_even_when_not_measured(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    results = _write(tmp_path / "r.json", {"token_bucket_take": 1.0})
    checkpoint.main(
        ["--phase", "0", "--results", str(results), "--env", str(_write(tmp_path / "e.json", {}))],
        root=root,
    )
    row = next(
        line
        for line in (root / "BENCHMARKS.md").read_text().splitlines()
        if line.startswith("| gw_throughput_hit_rps")
    )
    assert row.split("|")[-2].strip() == "not measured"


def test_median_runs_merges_the_per_metric_median_and_keeps_the_raw_values(tmp_path: Path) -> None:
    runs = [
        {"gw_overhead_miss_p50": 2.0, "gw_overhead_miss_p99": 30.0},
        {"gw_overhead_miss_p50": 3.0, "gw_overhead_miss_p99": 10.0},
        {"gw_overhead_miss_p50": 2.5, "gw_overhead_miss_p99": 14.0},
    ]
    paths = []
    for index, run in enumerate(runs):
        path = tmp_path / f"run{index}.json"
        benchlib.write_json(path, run)
        paths.append(str(path))
    out, raw = tmp_path / "results.json", tmp_path / "runs.json"
    benchlib.write_json(out, {"stack_idle_rss_total_mb": 266})

    assert median_runs.main([*paths, "--out", str(out), "--runs-out", str(raw)]) == 0

    assert benchlib.read_json(out) == {
        "stack_idle_rss_total_mb": 266,
        "gw_overhead_miss_p50": 2.5,
        "gw_overhead_miss_p99": 14.0,
    }
    assert benchlib.read_json(raw)["gw_overhead_miss_p99"] == [30.0, 10.0, 14.0]


# ------------------------------------------------------- budgets enforced from a later phase

DEFERRED_YAML = """\
metrics:
  gw_overhead_miss_p50: {unit: ms, direction: lower, budget: 3, spec: x}
  gw_overhead_miss_p99: {unit: ms, direction: lower, budget: 15, enforce_from_phase: 3, spec: x}
"""


@pytest.fixture
def deferred_file(tmp_path: Path) -> Path:
    path = tmp_path / "deferred.yaml"
    path.write_text(DEFERRED_YAML)
    return path


def test_enforce_from_phase_is_loaded_and_defaults_to_always(deferred_file: Path) -> None:
    budgets = benchlib.load_budgets(deferred_file)
    assert budgets["gw_overhead_miss_p99"].enforce_from_phase == 3
    assert budgets["gw_overhead_miss_p50"].enforce_from_phase == 0


@pytest.mark.parametrize(
    ("value", "phase", "status"),
    [
        (48.0, None, "over budget"),  # no phase given: everything is enforced
        (48.0, 1, "deferred"),
        (48.0, 2, "deferred"),
        (48.0, 3, "over budget"),
        (48.0, 4, "over budget"),
        (12.0, 1, "ok"),  # a met budget is simply ok, deferred or not
    ],
)
def test_judge_defers_a_budget_before_its_phase(
    deferred_file: Path, value: float, phase: int | None, status: str
) -> None:
    budget = benchlib.load_budgets(deferred_file)["gw_overhead_miss_p99"]
    assert benchlib.judge(value, budget, phase=phase) == status


def test_a_deferred_budget_does_not_fail_the_phase_but_the_others_still_do(
    tmp_path: Path, deferred_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = ["--budgets", str(deferred_file), "--enforce", "--phase", "1"]
    only_p99 = _write(tmp_path / "a.json", {"gw_overhead_miss_p99": 48.0})
    assert check_budgets.main([str(only_p99), *args]) == 0
    assert "deferred" in capsys.readouterr().out
    p50_too = _write(
        tmp_path / "b.json", {"gw_overhead_miss_p50": 4.0, "gw_overhead_miss_p99": 48.0}
    )
    assert check_budgets.main([str(p50_too), *args]) == 1
    assert check_budgets.main([str(only_p99), "--budgets", str(deferred_file), "--enforce"]) == 1


def test_checkpoint_table_marks_the_deferred_budget(deferred_file: Path) -> None:
    budgets = benchlib.load_budgets(deferred_file)
    checkpoints = {1: {"results": {"gw_overhead_miss_p50": 2.6, "gw_overhead_miss_p99": 48.0}}}
    table = checkpoint.render(budgets, checkpoints)
    row = next(line for line in table.splitlines() if line.startswith("| gw_overhead_miss_p99"))
    assert row.endswith("| deferred |")
