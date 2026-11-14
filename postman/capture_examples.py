"""Records a real sample response for every request in the Postman collection, against a running server.

    python -m src.server                                   # in another terminal (or the VS Code launch config)
    python postman/capture_examples.py --pause 60          # --pause: seconds after each LLM call (free-tier limits)
    python postman/build_collection.py                     # attaches postman/examples/samples.json as saved examples

Requests run in collection order, and the variables the Postman test scripts would set (lastRequestId,
conversationId, ingestRunId, evalRunId, replayKey) are carried forward the same way, so chained requests
(follow-up turn, idempotent replay, ingest run by id, eval run by id) record what they return in Postman.
--only "10 Semantic cache" records one folder (folder name prefix); other folders keep their saved samples.
"""
import argparse
import datetime
import json
import re
import sys
import time
import uuid
from pathlib import Path

import httpx

HERE = Path(__file__).parent
OUT = HERE / "examples" / "samples.json"
KEEP_HEADERS = {"content-type", "x-request-id", "idempotent-replayed", "x-response-time-ms", "retry-after"}
LLM_PATHS = ("/chat", "/chat/stream", "/eval/run")


def resolve(value, variables: dict):
    if isinstance(value, str):
        value = value.replace("{{$guid}}", str(uuid.uuid4()))
        return re.sub(r"\{\{(\w+)\}\}", lambda m: str(variables.get(m.group(1), m.group(0))), value)
    if isinstance(value, list):
        return [resolve(v, variables) for v in value]
    if isinstance(value, dict):
        return {k: resolve(v, variables) for k, v in value.items()}
    return value


def carry_variables(item: dict, url: str, resp: httpx.Response, body, variables: dict) -> None:
    """What the collection's test scripts store, so the next requests resolve the same way as in Postman."""
    if not isinstance(body, dict):
        return
    if url.endswith("/chat"):
        if resp.headers.get("x-request-id"):
            variables["lastRequestId"] = resp.headers["x-request-id"]
        for key, var in (("trace_id", "traceId"), ("conversation_id", "conversationId")):
            if body.get(key):
                variables[var] = body[key]
    if url.endswith("/admin/ingest/runs") and body.get("runs"):
        variables["ingestRunId"] = body["runs"][0]["run_id"]
    if url.endswith("/eval/run") and body.get("run_id"):
        variables["evalRunId"] = body["run_id"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pause", type=float, default=0, help="seconds to wait after a request that called an LLM")
    ap.add_argument("--only", default="", help="record only folders whose name starts with one of these "
                                                 "comma-separated prefixes, e.g. 02,03,12")
    ap.add_argument("--send-to", default="", help="actually send to this base URL (e.g. a second server on another "
                                                    "port); the saved request keeps the collection's URL")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    collection = json.loads((HERE / "Grounded-Card-Agent.postman_collection.json").read_text(encoding="utf-8"))
    variables = {v["key"]: v["value"] for v in collection["variable"]}
    samples = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    client = httpx.Client(timeout=httpx.Timeout(300.0, connect=5.0))
    recorded_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    for folder in collection["item"]:
        if args.only and not folder["name"].startswith(tuple(args.only.split(","))):
            continue
        for item in folder["item"]:
            if item["name"].startswith("Replay 1/2"):  # the request's pre-request script
                variables["replayKey"] = f"replay-{uuid.uuid4()}"
            request = resolve(item["request"], variables)
            headers = {h["key"]: h["value"] for h in request["header"]}
            headers["x-client-source"] = "postman-capture"
            content = request.get("body", {}).get("raw")
            url = request["url"]
            send_url = url.replace(variables["baseUrl"], args.send_to.rstrip("/"), 1) if args.send_to else url
            t0 = time.perf_counter()
            try:
                if url.endswith("/chat/stream"):
                    with client.stream(request["method"], send_url, headers=headers, content=content) as r:
                        text = "".join(r.iter_text())
                    resp, body = r, None
                else:
                    resp = client.request(request["method"], send_url, headers=headers, content=content)
                    text = resp.text
                    try:
                        body = resp.json()
                    except ValueError:
                        body = None
            except httpx.HTTPError as e:
                print(f"  !! {folder['name']} / {item['name']}: {type(e).__name__} {e}")
                continue
            ms = int((time.perf_counter() - t0) * 1000)
            carry_variables(item, url, resp, body, variables)
            samples[f"{folder['name']}/{item['name']}"] = {
                "recorded_at": recorded_at, "code": resp.status_code, "status": resp.reason_phrase,
                "is_json": body is not None,
                "body": json.dumps(body, indent=2, ensure_ascii=False) if body is not None else text,
                "headers": [{"key": k, "value": v} for k, v in resp.headers.items() if k.lower() in KEEP_HEADERS]
                           + [{"key": "x-recorded-latency-ms", "value": str(ms)}],
                "request": request,
            }
            status = body.get("status") if isinstance(body, dict) else ""
            llm_calls = (body.get("meta") or {}).get("llm_calls") if isinstance(body, dict) else None
            print(f"{resp.status_code} {ms:>6} ms  {folder['name'][:2]} {item['name'][:60]:<60} {status or ''}"
                  f"{' llm_calls=' + str(llm_calls) if llm_calls is not None else ''}", flush=True)
            OUT.parent.mkdir(exist_ok=True)
            OUT.write_text(json.dumps(samples, indent=2, ensure_ascii=False), encoding="utf-8")  # after each: resumable
            # Always pause after an LLM route: even a turn with llm_calls=0 may have hit open circuit breakers, and the
            # providers' per-minute budgets only recover with time.
            if args.pause and any(url.endswith(p) for p in LLM_PATHS) and not (isinstance(body, dict) and
                                                                                (body.get("meta") or {}).get("cache_hit")):
                time.sleep(args.pause)
    print(f"{len(samples)} samples in {OUT}")


if __name__ == "__main__":
    main()
