"""Runs web/scenario.json against the API. The end-to-end test, `make demo` and the recorder behind the
static web demo all use this one runner, and web/kit.js walks the same file in the browser."""
import datetime
import json
import pathlib
import re
import time

from . import db

PATH = db.ROOT / "web" / "scenario.json"


def load():
    return json.loads(PATH.read_text())


def fill(v, results):
    """Replace {{key.path}} with a value from an earlier response ({{now}} = current time)."""
    if isinstance(v, str):
        def sub(m):
            if m[1] == "now":
                return datetime.datetime.now(datetime.UTC).isoformat()
            if m[1].startswith("days_ago_"):
                return (datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=int(m[1][9:]))).isoformat()
            if m[1] == "loss_date":
                return (datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=2)).date().isoformat()
            out = results
            for k in m[1].split("."):
                out = out[int(k)] if isinstance(out, list) else out[k]
            return str(out)
        return re.sub(r"\{\{([\w.]+)\}\}", sub, v)
    if isinstance(v, list):
        return [fill(x, results) for x in v]
    if isinstance(v, dict):
        return {k: fill(x, results) for k, x in v.items()}
    return v


def run(client, scenario=None, drain=None, upto=None):
    """-> (results by key, transcript by key). `drain` runs queued jobs in-process (tests); otherwise we poll the worker."""
    scenario = scenario or load()
    results, transcript = {}, {}
    for step in scenario["steps"][:upto]:
        for c in step["calls"]:
            headers = {"Authorization": f"Bearer {scenario['tokens'][c['as']]}"}
            kw = {}
            if c["method"] != "GET":
                headers["Idempotency-Key"] = f"{scenario['run']}:{c['key']}"
                kw["json"] = fill(c.get("body", {}), results)
            res = client.request(c["method"], fill(c["path"], results), headers=headers, **kw)
            body = res.json()
            while res.status_code == 202 or (isinstance(body, dict) and body.get("status") in ("queued", "running") and "job_id" in body):
                drain() if drain else time.sleep(0.3)
                res = client.get(body.get("status_url") or f"/v1/jobs/{body['job_id']}", headers=headers)
                body = res.json()
            if isinstance(body, dict) and body.get("status") == "dead" and "job_id" in body:
                raise AssertionError(f"job for {c['key']} died: {body['error']}")
            if res.status_code >= 400 and not c.get("expect_error"):
                raise AssertionError(f"{c['method']} {c['path']} -> {res.status_code}: {body}")
            results[c["key"]] = body
            transcript[c["key"]] = {"status": res.status_code, "body": body}
    return results, transcript


def main():
    """python -m core.scenario [base_url] [--record]   run the demo against a running stack; --record rewrites web/demo.json"""
    import sys

    import httpx
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    scenario = load()
    with httpx.Client(base_url=args[0] if args else "http://127.0.0.1:8000", timeout=120) as client:
        results, transcript = run(client, scenario)
    for i, step in enumerate(scenario["steps"], 1):
        print(f"{i}. {step['title']}: " + ", ".join(f"{c['key']}={transcript[c['key']]['status']}" for c in step["calls"]))
    if "--record" in sys.argv:
        (db.ROOT / "web" / "demo.json").write_text(json.dumps(transcript, separators=(",", ":"), default=str))
        print("recorded web/demo.json")
    return results


if __name__ == "__main__":
    main()
