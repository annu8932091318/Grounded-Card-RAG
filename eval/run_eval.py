"""Runs eval/eval_cases.csv through the real agent, judges each case, writes JSON + markdown reports to
eval/results/, stores the run in Postgres, and exits non-zero when the gate fails (used by CI).
Usage: python -m eval.run_eval [--cases EV-001,EV-007] [--no-db]"""
import argparse
import csv
import json
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone

from eval.judges import judge
from src.const.config import config
from src.helper.prompt_loader import load_prompt
from src.modules.agent.agent import run_agent
from src.modules.llm.llm_router import chain
from src.modules.telemetry.logger import script_source_var
from src.modules.tools.errors import CustomerNotFound

CASES_PATH = config.root / "eval" / "eval_cases.csv"
RESULTS_DIR = config.root / "eval" / "results"
GATE_MIN_PASS_RATE = 85.0       # tune after the first real run; report the real number
GATE_MAX_UNEXPECTED_FAILS = 1

# per-case env overrides allowed in the csv `env` column -> (config attribute, parser)
ENV_OVERRIDES = {
    "TOOL_FAIL_RATE": ("tool_fail_rate", float),
    "LLM_FORCE_PRIMARY_FAIL": ("llm_force_primary_fail", lambda v: v in ("1", "true")),
    "LLM_DISABLED": ("llm_disabled", lambda v: v in ("1", "true")),
}


def load_cases(case_ids: list[str] | None = None) -> list[dict]:
    with open(CASES_PATH, encoding="utf-8", newline="") as f:
        cases = list(csv.DictReader(f))
    return [c for c in cases if not case_ids or c["case_id"] in case_ids]


def _apply_env(env: str) -> dict:
    previous = {}
    for pair in filter(None, (p.strip() for p in (env or "").split(";"))):
        name, _, value = pair.partition("=")
        attr, parse = ENV_OVERRIDES[name.strip()]
        previous[attr] = getattr(config, attr)
        setattr(config, attr, parse(value.strip()))
    return previous


def _git_sha() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], text=True, cwd=config.root).strip()
    except Exception:
        return "unknown"


def run_case(case: dict) -> dict:
    previous = _apply_env(case["env"])
    t0 = time.perf_counter()
    try:
        result = run_agent(case["customer_id"], case["question"])
    except CustomerNotFound:
        result = {"status": "not_found", "customer_message": "", "citations_flat": []}
    except Exception as e:  # a crash is a failed case, not a crashed run — but keep the evidence
        import traceback

        print(f"{case['case_id']} CRASH {type(e).__name__}: {e}\n{traceback.format_exc()}", flush=True)
        result = {"status": "error", "customer_message": "", "citations_flat": [], "error": f"{type(e).__name__}: {e}"}
    finally:
        for attr, value in previous.items():
            setattr(config, attr, value)
    passed, reasons = judge(case, result)
    meta = result.get("meta") or {}
    return {
        "case_id": case["case_id"], "source": case["source"], "expected": case["expected_behaviour"],
        "expect_fail": case["expect_fail"] == "true", "severity": case["severity"], "passed": passed,
        "actual_status": result.get("status"), "failure_reasons": reasons + ([result["error"]] if result.get("error") else []),
        "customer_message": result.get("customer_message", ""), "routing": result.get("routing"),
        "decision": result.get("decision"), "trace_id": result.get("trace_id"),
        "latency_ms": meta.get("latency_ms", int((time.perf_counter() - t0) * 1000)),
        "input_tokens": meta.get("tokens", {}).get("input", 0), "output_tokens": meta.get("tokens", {}).get("output", 0),
        "cost_usd": meta.get("cost_usd", 0.0), "flags": meta.get("flags", []),
    }


def summarise(results: list[dict]) -> dict:
    def rate(rows):
        return round(100 * sum(r["passed"] for r in rows) / len(rows), 2) if rows else 0.0

    expected_fail = [r for r in results if r["expect_fail"]]
    unexpected = [r for r in results if not r["passed"] and not r["expect_fail"]]
    critical = [r for r in results if not r["passed"] and r["severity"] == "critical"]
    costs = [r["cost_usd"] for r in results]
    latencies = sorted(r["latency_ms"] for r in results)
    pass_rate = rate(results)
    gate = (not critical and len(unexpected) <= GATE_MAX_UNEXPECTED_FAILS and pass_rate >= GATE_MIN_PASS_RATE)
    return {
        "started_at": datetime.now(timezone.utc).isoformat(), "git_sha": _git_sha(),
        "prompt_version": load_prompt()["version"], "model": ",".join(f"{p}:{m}" for m, p in chain()) or "static",
        "total": len(results), "passed": sum(r["passed"] for r in results), "pass_rate": pass_rate,
        "seed_pass_rate": rate([r for r in results if r["source"] == "seed"]),
        "own_pass_rate": rate([r for r in results if r["source"] == "own"]),
        "expected_fail_total": len(expected_fail),
        "expected_fail_hit": sum(not r["passed"] for r in expected_fail),
        "expected_fail_unexpectedly_passed": [r["case_id"] for r in expected_fail if r["passed"]],
        "unexpected_fail": len(unexpected), "unexpected_fail_cases": [(r["case_id"], r["trace_id"]) for r in unexpected],
        "critical_fail": len(critical), "critical_fail_cases": [r["case_id"] for r in critical],
        "cost_usd_mean_per_conversation": round(statistics.mean(costs), 6) if costs else 0,
        "cost_usd_p95_per_conversation": round(sorted(costs)[int(0.95 * (len(costs) - 1))], 6) if costs else 0,
        "latency_ms_p50": latencies[len(latencies) // 2] if latencies else 0,
        "latency_ms_p95": latencies[int(0.95 * (len(latencies) - 1))] if latencies else 0,
        "gate_passed": gate,
    }


def write_report_files(summary: dict, results: list[dict]) -> str:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    (RESULTS_DIR / f"run-{stamp}.json").write_text(json.dumps({"summary": summary, "results": results}, indent=2,
                                                              default=str), encoding="utf-8")
    s = summary
    lines = [
        f"# Eval run {stamp}", "",
        f"prompt `{s['prompt_version']}` · model `{s['model']}` · git `{s['git_sha']}` · "
        f"gate **{'PASSED' if s['gate_passed'] else 'FAILED'}**", "",
        "| metric | value |", "| --- | --- |",
        f"| overall pass rate | {s['passed']}/{s['total']} ({s['pass_rate']}%) |",
        f"| seed pass rate | {s['seed_pass_rate']}% |",
        f"| own-case pass rate | {s['own_pass_rate']}% |",
        f"| expected failures that failed as predicted | {s['expected_fail_hit']}/{s['expected_fail_total']} |",
        f"| expected failures that unexpectedly passed | {s['expected_fail_unexpectedly_passed'] or 'none'} |",
        f"| unexpected failures | {s['unexpected_fail']} {s['unexpected_fail_cases'] or ''} |",
        f"| critical failures | {s['critical_fail']} {s['critical_fail_cases'] or ''} |",
        f"| cost / conversation (mean, p95) | ${s['cost_usd_mean_per_conversation']} / ${s['cost_usd_p95_per_conversation']} |",
        f"| latency p50 / p95 | {s['latency_ms_p50']} ms / {s['latency_ms_p95']} ms |", "",
        "| case | src | expected | actual | pass | expect_fail | reasons | trace |", "| --- " * 8 + "|",
    ]
    for r in results:
        lines.append(f"| {r['case_id']} | {r['source']} | {r['expected']} | {r['actual_status']} | "
                     f"{'✅' if r['passed'] else '❌'} | {'yes' if r['expect_fail'] else ''} | "
                     f"{'; '.join(r['failure_reasons'])[:160]} | {(r['trace_id'] or '')[:8]} |")
    path = RESULTS_DIR / f"run-{stamp}.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def run_cases(case_ids: list[str] | None = None, write_reports: bool = True, persist: bool = True,
              pause_s: float = 0.0):
    script_source_var.set("eval")  # every case is one req_res_log row with source='eval'
    saved_cache_ttl = config.answer_cache_ttl_s
    config.answer_cache_ttl_s = 0  # WHY: cases must be independent — no answer reuse between them
    try:
        results = []
        for i, case in enumerate(load_cases(case_ids)):
            if i and pause_s:
                time.sleep(pause_s)  # WHY: free-tier per-minute quotas; changes test pacing only, not agent behaviour
            r = run_case(case)
            print(f"{r['case_id']} {'PASS' if r['passed'] else 'FAIL'} status={r['actual_status']} "
                  f"{'; '.join(r['failure_reasons'])}", flush=True)
            results.append(r)
    finally:
        config.answer_cache_ttl_s = saved_cache_ttl
    summary = summarise(results)
    if write_reports:
        summary["report"] = write_report_files(summary, results)
    if write_reports:
        summary["run_id"] = summary["report"].replace("\\", "/").rsplit("/", 1)[-1].removesuffix(".md")
    return summary, results


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", help="comma-separated case ids")
    parser.add_argument("--no-db", action="store_true")
    parser.add_argument("--pause", type=float, default=0.0, help="seconds between cases (rate-limited providers)")
    args = parser.parse_args()
    summary, _ = run_cases(args.cases.split(",") if args.cases else None, persist=not args.no_db, pause_s=args.pause)
    print(json.dumps(summary, indent=2, default=str))
    sys.exit(0 if summary["gate_passed"] else 1)


if __name__ == "__main__":
    main()
