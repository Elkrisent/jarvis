#!/usr/bin/env python3
"""Write env.json: where and how a benchmark ran (IMPLEMENTATION §C.3).

write_env.py --out build/bench/env.json --profile default --param duration=10s
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Any

from benchlib import write_json


def _run(*cmd: str) -> str | None:
    if shutil.which(cmd[0]) is None:
        return None
    try:
        return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def cpu_model(cpuinfo: str) -> str:
    for line in cpuinfo.splitlines():
        if line.lower().startswith("model name"):
            return line.split(":", 1)[1].strip()
    return platform.processor() or "unknown"


def ram_mb(meminfo: str) -> int:
    for line in meminfo.splitlines():
        if line.startswith("MemTotal:"):
            return int(line.split()[1]) // 1024
    return 0


def collect(
    profile: str, params: dict[str, str], environ: dict[str, str] | None = None
) -> dict[str, Any]:
    env = dict(os.environ) if environ is None else environ
    hosted = (
        env.get("GITHUB_ACTIONS") == "true" and env.get("RUNNER_ENVIRONMENT") == "github-hosted"
    )
    cpuinfo = Path("/proc/cpuinfo").read_text() if Path("/proc/cpuinfo").exists() else ""
    meminfo = Path("/proc/meminfo").read_text() if Path("/proc/meminfo").exists() else ""
    return {
        "commit": env.get("GITHUB_SHA") or _run("git", "rev-parse", "HEAD") or "unknown",
        "branch": env.get("GITHUB_REF_NAME")
        or _run("git", "rev-parse", "--abbrev-ref", "HEAD")
        or "unknown",
        "date": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "runner": "github-hosted" if hosted else "local",
        "cpu_model": cpu_model(cpuinfo),
        "cpu_cores": os.cpu_count() or 0,
        "ram_mb": ram_mb(meminfo),
        "os": platform.platform(),
        "python": platform.python_version(),
        "node": _run("node", "--version"),
        "docker": _run("docker", "--version"),
        "jarvis_profile": profile,
        "scenario_params": params,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--profile", default="default")
    ap.add_argument("--param", action="append", default=[], metavar="KEY=VALUE")
    args = ap.parse_args(argv)
    params = dict(p.split("=", 1) for p in args.param)
    write_json(args.out, collect(args.profile, params))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
