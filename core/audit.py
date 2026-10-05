"""Append-only audit log. Each row carries the SHA-256 of the previous row, so edits or gaps are detectable."""
import datetime
import hashlib
import json

from psycopg.types.json import Jsonb

GENESIS = "0" * 64


def _digest(prev, tenant_id, actor_id, action, rtype, rid, detail, at):
    line = json.dumps([prev, str(tenant_id), str(actor_id), action, rtype, str(rid), detail,
                       at.astimezone(datetime.UTC).isoformat()], sort_keys=True, default=str)
    return hashlib.sha256(line.encode()).hexdigest()


def record(c, ctx, action, resource_type, resource_id, detail=None):
    detail = json.loads(json.dumps(detail or {}, default=str))
    c.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", [f"audit:{ctx.tenant_id}"])     # one writer per tenant chain
    last = c.execute("SELECT hash FROM audit_events WHERE tenant_id = %s ORDER BY id DESC LIMIT 1", [ctx.tenant_id]).fetchone()
    prev = last["hash"] if last else GENESIS
    at = datetime.datetime.now(datetime.UTC)
    c.execute("""INSERT INTO audit_events (tenant_id, actor_id, action, resource_type, resource_id, detail, at, prev_hash, hash)
                 VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
              [ctx.tenant_id, ctx.actor_id, action, resource_type, str(resource_id), Jsonb(detail), at, prev,
               _digest(prev, ctx.tenant_id, ctx.actor_id, action, resource_type, resource_id, detail, at)])


def verify(c, tenant_id):
    """Recompute the chain. True only if no row was altered, removed or inserted out of band."""
    prev = GENESIS
    for r in c.execute("SELECT * FROM audit_events WHERE tenant_id = %s ORDER BY id", [tenant_id]):
        if r["prev_hash"] != prev or r["hash"] != _digest(prev, r["tenant_id"], r["actor_id"], r["action"],
                                                          r["resource_type"], r["resource_id"], r["detail"], r["at"]):
            return False
        prev = r["hash"]
    return True
