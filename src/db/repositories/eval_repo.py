"""eval_runs / eval_results."""
import json

from src.db.index import query, query_one, transaction


def save_run(summary: dict, results: list[dict]) -> str:
    with transaction() as conn:
        row = conn.execute(
            """INSERT INTO eval_runs (git_sha, prompt_version, model, total, passed, expected_fail_total,
                 expected_fail_hit, unexpected_fail, critical_fail, pass_rate, gate_passed)
               VALUES (%(git_sha)s, %(prompt_version)s, %(model)s, %(total)s, %(passed)s, %(expected_fail_total)s,
                 %(expected_fail_hit)s, %(unexpected_fail)s, %(critical_fail)s, %(pass_rate)s, %(gate_passed)s)
               RETURNING id""",
            summary,
        ).fetchone()
        run_id = row["id"]
        for r in results:
            conn.execute(
                """INSERT INTO eval_results (run_id, case_id, passed, expect_fail, severity, actual_status,
                     failure_reasons, latency_ms, input_tokens, output_tokens, trace_id)
                   VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s)""",
                (run_id, r["case_id"], r["passed"], r["expect_fail"], r["severity"], r["actual_status"],
                 json.dumps(r["failure_reasons"]), r["latency_ms"], r["input_tokens"], r["output_tokens"],
                 r["trace_id"]),
            )
    return str(run_id)


def list_runs(limit: int = 20) -> list[dict]:
    return query("SELECT * FROM eval_runs ORDER BY started_at DESC LIMIT %s", (limit,))


def get_run(run_id: str) -> dict | None:
    run = query_one("SELECT * FROM eval_runs WHERE id = %s", (run_id,))
    if run:
        run["results"] = query("SELECT * FROM eval_results WHERE run_id = %s ORDER BY case_id", (run_id,))
    return run
