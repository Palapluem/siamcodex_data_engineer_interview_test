"""Measure /reports/daily under the contract's stated load: 8 clients, 30s.

Host-side. Reports p50/p95/p99, error count and throughput, and samples container
CPU and memory while the load runs so the two are measured together rather than
inferred from separate runs.

    python submission/tools/loadtest.py
    python submission/tools/loadtest.py --clients 8 --seconds 30
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOKENS = json.loads((ROOT / ".runtime" / "client" / "test_tokens.json").read_text())
BASE = "http://127.0.0.1:8088"
EVIDENCE = ROOT.parent / "evidence"

_stop = threading.Event()


def worker(path: str, caller: str, latencies: list[float], errors: list[int]) -> None:
    token = TOKENS[caller]
    while not _stop.is_set():
        started = time.perf_counter()
        request = urllib.request.Request(BASE + path)
        request.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                response.read()
                if response.status != 200:
                    errors.append(response.status)
        except Exception:
            errors.append(0)
        latencies.append((time.perf_counter() - started) * 1000)


def sample_stats(samples: list[dict]) -> None:
    """docker stats snapshots for both services while the load runs."""
    while not _stop.is_set():
        result = subprocess.run(
            ["docker", "stats", "--no-stream", "--format", "{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}"],
            capture_output=True, text=True, timeout=30,
        )
        for line in result.stdout.strip().splitlines():
            parts = line.split("\t")
            if len(parts) == 3 and "de-interview-submission" in parts[0]:
                name, cpu, mem = parts
                samples.append({
                    "name": name.replace("de-interview-submission-", "").rstrip("-1"),
                    "cpu_percent": float(cpu.rstrip("%")),
                    "mem_mib": _mem_to_mib(mem.split("/")[0].strip()),
                })
        time.sleep(1.0)


def _mem_to_mib(value: str) -> float:
    number = float("".join(c for c in value if c.isdigit() or c == "."))
    unit = "".join(c for c in value if c.isalpha()).upper()
    return {"B": number / 1_048_576, "KIB": number / 1024, "MIB": number,
            "GIB": number * 1024}.get(unit, number)


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(p / 100 * (len(ordered) - 1))))
    return ordered[index]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--clients", type=int, default=8)
    parser.add_argument("--seconds", type=float, default=30.0)
    parser.add_argument("--caller", default="analyst_all")
    parser.add_argument("--path", default="/reports/daily")
    args = parser.parse_args()

    latencies: list[float] = []
    errors: list[int] = []
    samples: list[dict] = []

    print(f"{args.clients} clients x {args.seconds:.0f}s against {args.path} as {args.caller}")
    _stop.clear()
    threads = [threading.Thread(target=worker, args=(args.path, args.caller, latencies, errors),
                                daemon=True) for _ in range(args.clients)]
    monitor = threading.Thread(target=sample_stats, args=(samples,), daemon=True)

    started = time.perf_counter()
    for thread in threads:
        thread.start()
    monitor.start()
    time.sleep(args.seconds)
    _stop.set()
    for thread in threads:
        thread.join(timeout=35)
    monitor.join(timeout=5)
    elapsed = time.perf_counter() - started

    report = {
        "measured_at": datetime.now(UTC).isoformat(),
        "path": args.path, "caller": args.caller,
        "clients": args.clients, "duration_seconds": round(elapsed, 1),
        "requests": len(latencies),
        "throughput_rps": round(len(latencies) / elapsed, 1),
        "errors": len(errors),
        "latency_ms": {
            "p50": round(percentile(latencies, 50), 1),
            "p95": round(percentile(latencies, 95), 1),
            "p99": round(percentile(latencies, 99), 1),
            "max": round(max(latencies), 1) if latencies else 0.0,
            "mean": round(statistics.fmean(latencies), 1) if latencies else 0.0,
        },
        "resources": {},
    }

    for name in {s["name"] for s in samples}:
        rows = [s for s in samples if s["name"] == name]
        report["resources"][name] = {
            "samples": len(rows),
            "cpu_percent_mean": round(statistics.fmean(r["cpu_percent"] for r in rows), 1),
            "cpu_percent_peak": round(max(r["cpu_percent"] for r in rows), 1),
            "mem_mib_mean": round(statistics.fmean(r["mem_mib"] for r in rows), 1),
            "mem_mib_peak": round(max(r["mem_mib"] for r in rows), 1),
        }

    print(json.dumps(report, indent=2))
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE / "loadtest.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nWritten to {EVIDENCE / 'loadtest.json'}")

    target = 500.0
    verdict = "PASS" if report["latency_ms"]["p95"] <= target and not errors else "FAIL"
    print(f"p95 {report['latency_ms']['p95']}ms vs {target}ms target, "
          f"{len(errors)} errors -> {verdict}")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
