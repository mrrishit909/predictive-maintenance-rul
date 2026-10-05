"""Postgres job queue: enqueue inside the caller's transaction, claim with SKIP LOCKED, retry with backoff.

    python -m core.jobs <module that registers handlers>     # the worker process
"""
import importlib
import sys
import time
import traceback
import uuid

from fastapi.encoders import jsonable_encoder
from psycopg.types.json import Jsonb

from . import db

HANDLERS = {}
VISIBILITY = "5 minutes"     # a job 'running' longer than this is assumed to belong to a dead worker


def handler(kind):
    def register(fn):
        HANDLERS[kind] = fn
        return fn
    return register


def enqueue(c, ctx, kind, payload, max_attempts=4):
    job_id = uuid.uuid7()
    c.execute("INSERT INTO jobs (id, tenant_id, kind, payload, max_attempts) VALUES (%s,%s,%s,%s,%s)",
              [job_id, ctx.tenant_id, kind, Jsonb({**payload, "actor_id": str(ctx.actor_id)}), max_attempts])
    return {"job_id": str(job_id), "kind": kind, "status": "queued", "status_url": f"/v1/jobs/{job_id}"}


def work_one():
    """Claim and run one job. Returns False when the queue is empty."""
    with db.tx() as c:
        job = c.execute(f"""
            UPDATE jobs SET status = 'running', attempts = attempts + 1, started_at = now()
             WHERE id = (SELECT id FROM jobs
                          WHERE run_after <= now()
                            AND (status = 'queued' OR (status = 'running' AND started_at < now() - interval '{VISIBILITY}'))
                          ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1)
            RETURNING *""").fetchone()
    if not job:
        return False
    try:
        # the handler's writes and the 'succeeded' mark commit together: a crash re-runs the job from a clean state
        with db.tx(job["tenant_id"]) as c:
            result = HANDLERS[job["kind"]](c, job)
            c.execute("UPDATE jobs SET status = 'succeeded', progress = 1, result = %s, error = NULL, finished_at = now() WHERE id = %s",
                      [Jsonb(jsonable_encoder(result)), job["id"]])
    except Exception as e:
        dead = job["attempts"] >= job["max_attempts"]
        with db.tx() as c:
            c.execute("""UPDATE jobs SET status = %s, error = %s, finished_at = CASE WHEN %s THEN now() END,
                                run_after = now() + make_interval(secs => power(2, attempts)) WHERE id = %s""",
                      ["dead" if dead else "queued", f"{type(e).__name__}: {e}", dead, job["id"]])
        if not dead:
            traceback.print_exc()
    return True


def drain():
    """Run everything that is due now (tests and `make demo` use this instead of a worker process)."""
    n = 0
    while work_one():
        n += 1
    return n


def main(module):
    importlib.import_module(module)      # importing the app registers its handlers
    print(f"worker up, handlers: {sorted(HANDLERS)}", flush=True)
    while True:
        if not work_one():
            time.sleep(0.5)


if __name__ == "__main__":
    from core.jobs import main as run_worker      # not this __main__ copy: handlers register on core.jobs
    run_worker(sys.argv[1])
