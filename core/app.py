"""HTTP kit: bearer-token auth with per-role permissions, RFC 7807 errors, Idempotency-Key replay,
job status, audit read-back, request logs as JSON lines and a Prometheus /metrics endpoint."""
import dataclasses
import hashlib
import json
import pathlib
import sys
import time
import uuid
from collections import Counter

from fastapi import Depends, FastAPI, Header, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from psycopg.types.json import Jsonb

from . import audit, db

MAX_BODY = 5 * 1024 * 1024
BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 0.75, 1, 2.5, 5)


class Problem(Exception):
    """An error the client should see, rendered as application/problem+json with a stable `code`."""

    def __init__(self, status, code, detail=""):
        self.status, self.code, self.detail = status, code, detail


@dataclasses.dataclass
class Ctx:
    tenant_id: uuid.UUID
    actor_id: uuid.UUID
    actor_name: str
    role: str


def problem(status, code, detail=""):
    return JSONResponse({"type": f"https://errors.example/{code}", "title": code.replace("_", " "), "status": status,
                         "code": code, "detail": detail}, status, media_type="application/problem+json")


def create_app(service, permissions, version="1.0.0"):
    """`permissions` maps role -> set of permission strings; anything not listed is denied."""
    app = FastAPI(title=service, version=version)
    requests, latency, latency_sum = Counter(), Counter(), Counter()

    @app.exception_handler(Problem)
    async def _problem(_, e):
        return problem(e.status, e.code, e.detail)

    @app.exception_handler(RequestValidationError)
    async def _invalid(_, e):
        return problem(422, "validation_failed", "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()))

    @app.middleware("http")
    async def _observe(request: Request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex
        if int(request.headers.get("content-length") or 0) > MAX_BODY:
            return problem(413, "payload_too_large", f"limit is {MAX_BODY} bytes")
        t0 = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception as e:   # never leak a stack trace to the client; the log line has it
            print(json.dumps({"level": "error", "request_id": rid, "error": repr(e)}), file=sys.stderr, flush=True)
            response = problem(500, "internal_error", f"request {rid}")
        dt = time.perf_counter() - t0
        route = getattr(request.scope.get("route"), "path", "unmatched")
        requests[(request.method, route, response.status_code)] += 1
        latency_sum[route] += dt
        for b in BUCKETS:
            if dt <= b:
                latency[(route, b)] += 1
        latency[(route, "+Inf")] += 1
        ctx = getattr(request.state, "ctx", None)
        if route not in ("/metrics", "/healthz") and not route.startswith("/ui"):
            print(json.dumps({"level": "info", "request_id": rid, "trace_id": rid, "tenant_id": str(ctx.tenant_id) if ctx else None,
                              "actor_id": str(ctx.actor_id) if ctx else None, "method": request.method, "route": route,
                              "status": response.status_code, "ms": round(dt * 1000, 1)}), flush=True)
        response.headers["x-request-id"] = rid
        return response

    def auth(perm):
        def dependency(request: Request, authorization: str | None = Header(None)):
            if not authorization or not authorization.startswith("Bearer "):
                raise Problem(401, "unauthenticated", "send Authorization: Bearer <token>")
            digest = hashlib.sha256(authorization[7:].encode()).hexdigest()
            with db.tx() as c:
                row = c.execute("SELECT tenant_id, actor_id, actor_name, role FROM api_tokens WHERE token_sha256 = %s", [digest]).fetchone()
            if not row:
                raise Problem(401, "unauthenticated", "unknown token")
            ctx = Ctx(**row)
            request.state.ctx = ctx
            if perm not in permissions.get(ctx.role, ()):      # deny by default
                raise Problem(403, "forbidden", f"role '{ctx.role}' lacks permission '{perm}'")
            return ctx
        return dependency

    app.state.auth = auth

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        with db.tx() as c:
            c.execute("SELECT 1")
        return {"service": service, "version": version, "status": "ok"}

    @app.get("/metrics", include_in_schema=False)
    def metrics():
        out = ["# TYPE http_requests_total counter"]
        out += [f'http_requests_total{{method="{m}",route="{r}",status="{s}"}} {n}' for (m, r, s), n in sorted(requests.items())]
        out.append("# TYPE http_request_duration_seconds histogram")
        out += [f'http_request_duration_seconds_bucket{{route="{r}",le="{b}"}} {n}' for (r, b), n in sorted(latency.items(), key=str)]
        out += [f'http_request_duration_seconds_sum{{route="{r}"}} {s:.6f}' for r, s in sorted(latency_sum.items())]
        with db.tx() as c:
            out.append("# TYPE jobs gauge")
            for r in c.execute("SELECT status, count(*) AS n FROM jobs GROUP BY status"):
                out.append(f'jobs{{status="{r["status"]}"}} {r["n"]}')
            age = c.execute("SELECT coalesce(extract(epoch FROM now() - min(created_at)), 0) AS s FROM jobs WHERE status = 'queued'").fetchone()["s"]
            out.append(f"jobs_oldest_queued_seconds {float(age):.1f}")
        st = db.pool().get_stats()
        out.append(f'db_pool_in_use {st.get("pool_size", 0) - st.get("pool_available", 0)}')
        return PlainTextResponse("\n".join(out) + "\n")

    any_role = auth("jobs:read")

    @app.get("/v1/jobs/{job_id}", tags=["platform"], summary="Status, attempts and result of a long-running job")
    def job_status(job_id: uuid.UUID, ctx: Ctx = Depends(any_role)):
        with db.tx(ctx.tenant_id) as c:
            job = c.execute("""SELECT id AS job_id, kind, status, progress, attempts, max_attempts, error, result, created_at, started_at, finished_at
                                 FROM jobs WHERE id = %s""", [job_id]).fetchone()
        if not job:
            raise Problem(404, "job_not_found")
        return jsonable_encoder(job)

    @app.get("/v1/jobs", tags=["platform"], summary="Jobs by status; status=dead is the dead-letter view")
    def job_list(status: str = "dead", ctx: Ctx = Depends(any_role)):
        with db.tx(ctx.tenant_id) as c:
            return jsonable_encoder(c.execute("""SELECT id AS job_id, kind, status, attempts, error, created_at, finished_at
                                                   FROM jobs WHERE status = %s ORDER BY created_at DESC LIMIT 100""", [status]).fetchall())

    @app.get("/v1/audit", tags=["platform"], summary="Latest audit events and whether the hash chain verifies")
    def audit_log(limit: int = 50, ctx: Ctx = Depends(auth("audit:read"))):
        with db.tx(ctx.tenant_id) as c:
            rows = c.execute("""SELECT a.id, a.at, a.action, a.resource_type, a.resource_id, a.detail, a.hash, a.actor_id
                                  FROM audit_events a ORDER BY a.id DESC LIMIT %s""", [min(limit, 500)]).fetchall()
            return jsonable_encoder({"chain_valid": audit.verify(c, ctx.tenant_id), "events": rows})

    web = pathlib.Path(db.ROOT / "web")
    if web.exists():
        app.mount("/ui", StaticFiles(directory=web, html=True), name="ui")
    return app


def run(ctx, idempotency_key, request_body, fn):
    """Run one write in a tenant transaction. `fn(c)` returns (status, body).

    With an Idempotency-Key the key row is inserted first in the same transaction: a concurrent retry
    blocks on it, then finds the stored response and returns that instead of running the work twice.
    """
    digest = hashlib.sha256(json.dumps(jsonable_encoder(request_body), sort_keys=True).encode()).hexdigest()
    with db.tx(ctx.tenant_id) as c:
        if idempotency_key:
            fresh = c.execute("""INSERT INTO idempotency_keys (tenant_id, key, request_hash) VALUES (%s,%s,%s)
                                 ON CONFLICT DO NOTHING RETURNING key""", [ctx.tenant_id, idempotency_key, digest]).fetchone()
            if not fresh:
                prior = c.execute("SELECT request_hash, status, body FROM idempotency_keys WHERE tenant_id = %s AND key = %s",
                                  [ctx.tenant_id, idempotency_key]).fetchone()
                if prior["request_hash"] != digest:
                    raise Problem(422, "idempotency_key_reused", "this key was used with a different request body")
                return JSONResponse(prior["body"], prior["status"], headers={"Idempotent-Replay": "true"})
        status, body = fn(c)
        body = jsonable_encoder(body)
        if idempotency_key:
            c.execute("UPDATE idempotency_keys SET status = %s, body = %s WHERE tenant_id = %s AND key = %s",
                      [status, Jsonb(body), ctx.tenant_id, idempotency_key])
        return JSONResponse(body, status)
