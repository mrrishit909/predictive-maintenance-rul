"""Public API (modular monolith). Blueprint services map to: stream-ingestion + feature-service + anomaly-service + rul-service
(pm/store.py, pm/dsp.py, pm/models.py), maintenance-optimizer (pm/optimize.py), asset-registry, alert-service and
historian-api (this file). The machines themselves are simulated (pm/sim.py).

    uvicorn pm.api:app          python -m core.jobs pm.api      # the worker
"""
import base64
import datetime
import pickle
import uuid

import numpy as np
from fastapi import Depends, Header
from fastapi.encoders import jsonable_encoder
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from core import audit, db, jobs
from core.app import Ctx, Problem, create_app, run

from . import dsp, models, optimize, sim, store
from .seed import asset_rng

READ = {"assets:read", "jobs:read"}
PERMISSIONS = {"technician": READ | {"signals:write", "alerts:ack", "workorders:write", "simulate"}, "planner": READ | {"maintenance:plan", "workorders:write", "alerts:ack", "simulate"},
               "admin": READ | {"signals:write", "alerts:ack", "workorders:write", "maintenance:plan", "simulate", "audit:read"}}
app = create_app("predictive-maintenance-rul", PERMISSIONS)
auth = app.state.auth
IdemKey = Header(None, alias="Idempotency-Key")
_bundle = {}
ASSET = """SELECT a.*, s.sim_clock, s.tenant_id, s.id AS site, (SELECT id FROM sensors WHERE asset_id = a.id AND sensor_type = 'accelerometer') AS sensor_id
             FROM assets a JOIN sites s ON s.id = a.site_id"""


def bundle():
    if "b" not in _bundle:
        with db.tx() as c:
            _bundle["b"] = pickle.loads(c.execute("SELECT artifact FROM model_registry WHERE name = 'condition-models' AND state = 'production'").fetchone()["artifact"])      # our own training output
    return _bundle["b"]


def get_asset(c, asset_id, lock=False):
    a = c.execute(ASSET + " WHERE a.id = %s" + (" FOR UPDATE OF a" if lock else ""), [asset_id]).fetchone()      # sites carry the tenant; RLS hides other tenants' plants
    if not a:
        raise Problem(404, "asset_not_found")
    return a


def latest(c, asset_id):
    r = c.execute("SELECT * FROM rul_predictions WHERE asset_id = %s ORDER BY predicted_at DESC LIMIT 1", [asset_id]).fetchone()
    s = c.execute("SELECT score, evidence FROM anomaly_scores WHERE asset_id = %s ORDER BY scored_at DESC LIMIT 1", [asset_id]).fetchone()
    return r, s


# ---------------------------------------------------------------- stream-ingestion

class Window(BaseModel):
    started_at: datetime.datetime
    shaft_hz: float = Field(gt=1, lt=200)
    samples_base64: str = Field(description=f"{sim.N} little-endian float32 acceleration samples at {sim.FS} Hz")


class SignalsIn(BaseModel):
    sensor_id: uuid.UUID
    windows: list[Window] = Field(min_length=1, max_length=20)


@app.post("/v1/signals:batch", status_code=201, tags=["ingestion"], summary="Upload waveform windows; features, anomaly score and RUL are updated in the same transaction")
def signals(body: SignalsIn, ctx: Ctx = Depends(auth("signals:write")), idem: str | None = IdemKey):
    def work(c):
        a = c.execute(ASSET + " JOIN sensors x ON x.asset_id = a.id WHERE x.id = %s FOR UPDATE OF a", [body.sensor_id]).fetchone()
        if not a:
            raise Problem(404, "sensor_not_found")
        wins = []
        for w in body.windows:
            try:
                x = np.frombuffer(base64.b64decode(w.samples_base64, validate=True), "<f4")
            except Exception as e:
                raise Problem(422, "invalid_waveform", str(e)) from e
            if len(x) != sim.N or not np.isfinite(x).all() or np.abs(x).max() > 100:
                raise Problem(422, "invalid_waveform", f"expected {sim.N} finite samples within ±100 g, got {len(x)}")
            wins.append((w.started_at if w.started_at.tzinfo else w.started_at.replace(tzinfo=datetime.UTC), w.shaft_hz, x))
        wins.sort(key=lambda t: t[0])
        counts, state = store.ingest(c, ctx.tenant_id, a, body.sensor_id, wins, bundle())
        return 201, {**counts, "asset": a["name"], "state": state}
    return run(ctx, idem, body, work)


class RunIn(BaseModel):
    hours: int = Field(ge=6, le=720)


@app.post("/v1/simulator:run", status_code=202, tags=["simulator"], summary="Advance the simulated plant; every machine's new windows go through ingestion (job)")
def simulate(body: RunIn, ctx: Ctx = Depends(auth("simulate")), idem: str | None = IdemKey):
    return run(ctx, idem, body, lambda c: (202, jobs.enqueue(c, ctx, "sim.run", {"hours": body.hours})))


@jobs.handler("sim.run")
def sim_job(c, job):
    hours, b = job["payload"]["hours"], bundle()
    site = c.execute("SELECT * FROM sites FOR UPDATE").fetchone()
    stored, failed, alerts_before = 0, [], c.execute("SELECT count(*) AS n FROM alerts").fetchone()["n"]
    for a in c.execute(ASSET + " ORDER BY a.name").fetchall():
        s = a["sim"]
        end = min(s["age_h"] + hours, s["life_h"])
        ages, speeds, waves = sim.run_to_failure(asset_rng(a["id"], s["age_h"]), s["mode"], s["life_h"], s["shaft_hz"], until_h=end, start_h=s["age_h"])
        t0 = site["sim_clock"]
        counts, _ = store.ingest(c, a["tenant_id"], a, a["sensor_id"], [(t0 + datetime.timedelta(hours=float(x - s["age_h"])), sp, w) for x, sp, w in zip(ages, speeds, waves)], b)
        stored += counts["stored"]
        if end >= s["life_h"]:
            failed.append(a["name"])
        c.execute("UPDATE assets SET sim = %s WHERE id = %s", [Jsonb({**s, "age_h": s["age_h"] + hours}), a["id"]])
    clock = site["sim_clock"] + datetime.timedelta(hours=hours)
    c.execute("UPDATE sites SET sim_clock = %s WHERE id = %s", [clock, site["id"]])
    ctx = Ctx(job["tenant_id"], uuid.UUID(job["payload"]["actor_id"]), "worker", "system")
    audit.record(c, ctx, "plant.simulated", "site", site["id"], {"hours": hours, "windows": stored, "models": models.VERSION})
    return {"hours": hours, "plant_clock": clock, "windows_stored": stored, "samples": stored * sim.N, "new_alerts": c.execute("SELECT count(*) AS n FROM alerts").fetchone()["n"] - alerts_before,
            "failed_in_service": failed, "feature_set": dsp.VERSION, "models": models.VERSION}


# ---------------------------------------------------------------- asset-registry + historian

@app.get("/v1/assets", tags=["assets"], summary="Fleet list with current health and remaining life")
def assets(name: str | None = None, ctx: Ctx = Depends(auth("assets:read"))):
    with db.tx(ctx.tenant_id) as c:
        rows = c.execute(ASSET + " WHERE %s::text IS NULL OR a.name = %s ORDER BY a.name", [name, name]).fetchall()
        out = []
        for a in rows:
            r, s = latest(c, a["id"])
            out.append({"id": a["id"], "name": a["name"], "asset_type": a["asset_type"], "criticality": a["criticality"], "sensor_id": a["sensor_id"],
                        "anomaly_score": s and round(float(s["score"]), 2), "threshold": s and round(s["evidence"]["threshold"], 2), "anomalous": bool(s and float(s["score"]) > s["evidence"]["threshold"]),
                        "rul_p50_h": r and round(float(r["hours_p50"])), "rul_p10_h": r and round(float(r["hours_p10"])), "rul_p90_h": r and round(float(r["hours_p90"])), "failure_mode": r and r["failure_mode"],
                        "open_alerts": c.execute("SELECT count(*) AS n FROM alerts WHERE asset_id = %s AND status = 'open'", [a["id"]]).fetchone()["n"]})
    return jsonable_encoder({"items": sorted(out, key=lambda x: x["rul_p50_h"] if x["rul_p50_h"] is not None else 1e9)})


@app.get("/v1/assets/{asset_id}/health", tags=["assets"], summary="Current health score, its history and active alerts")
def health(asset_id: uuid.UUID, ctx: Ctx = Depends(auth("assets:read"))):
    with db.tx(ctx.tenant_id) as c:
        a = get_asset(c, asset_id)
        hist = c.execute("SELECT scored_at, score, evidence FROM anomaly_scores WHERE asset_id = %s AND scored_at >= %s ORDER BY scored_at", [asset_id, a["overhauled_at"]]).fetchall()
        alerts = c.execute("SELECT id, opened_at, severity, message, status, acknowledged_at FROM alerts WHERE asset_id = %s ORDER BY opened_at DESC LIMIT 5", [asset_id]).fetchall()
    if not hist:
        raise Problem(409, "not_enough_data", f"the asset needs {models.BASE_N + 1} windows after an overhaul before it can be scored")
    thr = hist[-1]["evidence"]["threshold"]
    hours = lambda t: round((t - a["overhauled_at"]).total_seconds() / 3600, 1)      # noqa: E731
    return jsonable_encoder({"asset_id": asset_id, "name": a["name"], "as_of": hist[-1]["scored_at"], "hours_since_overhaul": hours(hist[-1]["scored_at"]), "score": round(float(hist[-1]["score"]), 2),
                             "threshold": round(thr, 2), "anomalous": float(hist[-1]["score"]) > thr, "model_version": models.VERSION, "relative_levels": hist[-1]["evidence"]["relative_levels"],
                             "series": {"hours": [hours(h["scored_at"]) for h in hist], "score": [round(float(h["score"]), 2) for h in hist]}, "alerts": alerts})


def _waveform(c, row):
    b = c.execute("SELECT data FROM waveform_blobs WHERE 'blob:' || sha256 = %s", [row["object_uri"]]).fetchone()
    return np.frombuffer(bytes(b["data"]), np.float16).astype(np.float64) if b else None


@app.get("/v1/assets/{asset_id}/spectrum", tags=["assets"], summary="Envelope and raw spectrum of the latest window, next to the healthy reference window")
def spectrum(asset_id: uuid.UUID, ctx: Ctx = Depends(auth("assets:read"))):
    with db.tx(ctx.tenant_id) as c:
        a = get_asset(c, asset_id)
        q = "SELECT id, started_at, shaft_hz, object_uri FROM signal_windows WHERE sensor_id = %s AND started_at >= %s ORDER BY started_at {} LIMIT 1"
        ref, cur = (c.execute(q.format(o), [a["sensor_id"], a["overhauled_at"]]).fetchone() for o in ("ASC", "DESC"))
        waves = [_waveform(c, r) for r in (ref, cur)]
    if any(w is None for w in waves):
        raise Problem(409, "waveform_not_retained", "the raw waveform for this window has been dropped by retention")
    freq = np.fft.rfftfreq(sim.N, 1 / sim.FS)
    lo = freq <= 400
    out = {"asset_id": asset_id, "name": a["name"], "feature_set": dsp.VERSION, "resolution_hz": float(freq[1]), "envelope_band_hz": [1500, 3500], "windows": []}
    for label, r, w in (("healthy reference", ref, waves[0]), ("latest", cur, waves[1])):
        env, raw = dsp.envelope_spectrum(w), dsp.spectrum(w)
        bpfo = sim.BPFO * r["shaft_hz"]
        out["windows"].append({"label": label, "started_at": r["started_at"], "shaft_hz": round(r["shaft_hz"], 2), "bpfo_hz": round(bpfo, 1),
                               "envelope": {"hz": np.round(freq[lo], 1).tolist(), "amplitude": np.round(env[lo], 4).tolist()},
                               "raw": {"hz": np.round(freq[::8], 0).tolist(), "amplitude": np.round(raw.reshape(-1)[:len(freq) // 8 * 8].reshape(-1, 8).max(axis=1), 4).tolist() + [0.0]},
                               "bpfo_amplitude": round(dsp._line(env, bpfo), 4), "features": dict(zip(dsp.NAMES, np.round(dsp.features(w, r["shaft_hz"]), 4).tolist()))})
    ref_a, cur_a = out["windows"][0]["bpfo_amplitude"], out["windows"][1]["bpfo_amplitude"]
    out["bpfo_growth"] = round(cur_a / max(ref_a, 1e-9), 1)
    return jsonable_encoder(out)


@app.get("/v1/assets/{asset_id}/rul", tags=["assets"], summary="Remaining-useful-life distribution, model evidence and the expected cost of doing nothing")
def rul(asset_id: uuid.UUID, ctx: Ctx = Depends(auth("assets:read"))):
    with db.tx(ctx.tenant_id) as c:
        a = get_asset(c, asset_id)
        hist = c.execute("SELECT predicted_at, hours_p10, hours_p50, hours_p90, failure_mode FROM rul_predictions WHERE asset_id = %s AND predicted_at >= %s ORDER BY predicted_at", [asset_id, a["overhauled_at"]]).fetchall()
        metrics = c.execute("SELECT metrics FROM model_registry WHERE name = 'condition-models'").fetchone()["metrics"]["rul"]
        _, s = latest(c, asset_id)
    if not hist:
        raise Problem(409, "not_enough_data", "no prediction yet for this asset")
    last, e = hist[-1], a["economics"]
    p10, p50, p90 = float(last["hours_p10"]), float(last["hours_p50"]), float(last["hours_p90"])
    unplanned = e["unplanned_h"] * e["downtime_cost_per_h"] + 2.5 * e["part_cost"] + 1.5 * e["labor_cost"]
    pf = {d: round(optimize.fail_by(d * 24, p10, p50, p90), 3) for d in (3, 7, 14)}
    hours = lambda t: round((t - a["overhauled_at"]).total_seconds() / 3600, 1)      # noqa: E731
    capped = p50 >= models.RUL_CAP - 20
    return jsonable_encoder({"asset_id": asset_id, "name": a["name"], "as_of": last["predicted_at"], "model_version": models.VERSION, "hours_p10": round(p10), "hours_p50": round(p50), "hours_p90": round(p90),
                             "beyond_horizon": capped, "note": f"the model does not look further than {models.RUL_CAP:.0f} hours" if capped else None, "failure_mode": last["failure_mode"],
                             "interval": {"nominal_coverage": 0.8, "held_out_coverage": metrics["coverage_conformal"], "conformal_adjustment_h": metrics["conformal_adjustment_h"]},
                             "evidence": s["evidence"]["relative_levels"],
                             "if_ignored": {"probability_of_failure_within_days": pf, "unplanned_failure_cost": round(unplanned, 2), "unplanned_downtime_h": e["unplanned_h"],
                                            "expected_loss_14_days": round(pf[14] * unplanned, 2), "planned_repair_cost_line_stopped": e["part_cost"] + e["labor_cost"],
                                            "planned_repair_cost_line_running": e["part_cost"] + e["labor_cost"] + e["repair_h"] * e["downtime_cost_per_h"]},
                             "history": {"hours": [hours(h["predicted_at"]) for h in hist], "p10": [round(float(h["hours_p10"])) for h in hist], "p50": [round(float(h["hours_p50"])) for h in hist],
                                         "p90": [round(float(h["hours_p90"])) for h in hist]}})


# ---------------------------------------------------------------- alert-service

@app.post("/v1/alerts/{alert_id}:ack", tags=["alerts"], summary="Acknowledge an alert")
def ack(alert_id: uuid.UUID, ctx: Ctx = Depends(auth("alerts:ack")), idem: str | None = IdemKey):
    def work(c):
        a = c.execute("""UPDATE alerts SET status = 'acknowledged', acknowledged_by = %s, acknowledged_at = now()
                          WHERE id = %s AND status = 'open' AND asset_id IN (SELECT a.id FROM assets a JOIN sites s ON s.id = a.site_id) RETURNING id, asset_id, message""", [ctx.actor_id, alert_id]).fetchone()
        if not a:
            raise Problem(404, "alert_not_found", "no open alert with that id")
        audit.record(c, ctx, "alert.acknowledged", "alert", alert_id, {"by": ctx.actor_name})
        return 200, {"id": alert_id, "status": "acknowledged", "acknowledged_by": ctx.actor_name, "message": a["message"]}
    return run(ctx, idem, {}, work)


# ---------------------------------------------------------------- maintenance-optimizer

class PlanIn(BaseModel):
    horizon_days: int = Field(14, ge=3, le=30)


def production_calendar(clock, horizon_days):
    """Candidate maintenance slots: one early-shift slot a day while the line runs, and a planned changeover (line stopped) on days 4 and 5."""
    slots = []
    for d in range(horizon_days):
        start = 6 + 24 * d
        slots.append({"start_h": start, "start_at": clock + datetime.timedelta(hours=start), "length_h": 4, "production_stopped": d in (4, 5), "technicians": 2,
                      "label": f"day {d + 1} {'changeover (line stopped)' if d in (4, 5) else 'early shift (line running)'}"})
    return slots


@app.post("/v1/maintenance:optimize", status_code=201, tags=["maintenance"], summary="Generate a maintenance schedule across the fleet and compare it with two simple policies")
def plan(body: PlanIn, ctx: Ctx = Depends(auth("maintenance:plan")), idem: str | None = IdemKey):
    def work(c):
        site = c.execute("SELECT * FROM sites").fetchone()
        mean_life = bundle()["mean_life"]
        fleet = []
        for a in c.execute(ASSET + " ORDER BY a.name").fetchall():
            r, s = latest(c, a["id"])
            if r:
                fleet.append({"id": str(a["id"]), "name": a["name"], "p10": float(r["hours_p10"]), "p50": float(r["hours_p50"]), "p90": float(r["hours_p90"]), "horizon_h": body.horizon_days * 24,
                              "mean_life_h": mean_life, "alarming": float(s["score"]) > s["evidence"]["threshold"], **a["economics"]})
        at_risk = [a for a in fleet if a["p50"] < models.RUL_CAP - 20]          # machines whose life is beyond what the model can see are left alone
        slots = production_calendar(site["sim_clock"], body.horizon_days)
        best = optimize.solve(at_risk, slots)
        compare = {"optimized": best, "run_to_failure": optimize.baseline(at_risk, slots, "run_to_failure"), "fix_at_first_opening": optimize.baseline(at_risk, slots, "earliest")}
        c.execute("DELETE FROM maintenance_windows WHERE status = 'proposed' AND asset_id IN (SELECT a.id FROM assets a JOIN sites s ON s.id = a.site_id)")
        ignore = {r["asset_id"]: r["expected"] for r in compare["run_to_failure"]["schedule"]}
        for r in best["schedule"]:
            w = None if r["window"] is None else slots[r["window"]]
            r["window"] = w and {"label": w["label"], "start_at": w["start_at"], "start_h": w["start_h"], "production_stopped": w["production_stopped"]}
            if w:
                r["maintenance_window_id"] = uuid.uuid7()
                c.execute("INSERT INTO maintenance_windows (id, asset_id, start_at, end_at, expected_cost, avoided_risk, status) VALUES (%s,%s,%s,%s,%s,%s,'proposed')",
                          [r["maintenance_window_id"], r["asset_id"], w["start_at"], w["start_at"] + datetime.timedelta(hours=w["length_h"]), r["expected"], ignore[r["asset_id"]] - r["expected"]])
        audit.record(c, ctx, "maintenance.planned", "site", site["id"], {"optimizer": optimize.VERSION, "assets_at_risk": len(at_risk), "expected_cost": best["total_expected_cost"],
                                                                         "run_to_failure_cost": compare["run_to_failure"]["total_expected_cost"]})
        return 201, {"plant_clock": site["sim_clock"], "horizon_days": body.horizon_days, "assets_monitored": len(fleet), "assets_at_risk": len(at_risk), "solver": best["solver"],
                     "schedule": best["schedule"], "calendar": [{k: s[k] for k in ("label", "start_at", "production_stopped", "technicians")} for s in slots[:8]],
                     "comparison": [{"policy": k, "total_expected_cost": v["total_expected_cost"], "expected_failure_cost": round(sum(r["failure"] for r in v["schedule"]), 2),
                                     "jobs_scheduled": sum(r["window"] is not None for r in v["schedule"])} for k, v in compare.items()]}
    return run(ctx, idem, body, work)


# ---------------------------------------------------------------- work orders + label capture

class WorkOrderIn(BaseModel):
    asset_id: uuid.UUID
    work_type: str = Field(pattern="^(bearing_replacement|balancing|alignment|lubrication|inspection)$")
    maintenance_window_id: uuid.UUID | None = None


@app.post("/v1/work-orders", status_code=201, tags=["work-orders"], summary="Create a work order (stands in for the CMMS sync)")
def create_work_order(body: WorkOrderIn, ctx: Ctx = Depends(auth("workorders:write")), idem: str | None = IdemKey):
    def work(c):
        a = get_asset(c, body.asset_id)
        wid = uuid.uuid7()
        ext = f"CMMS-{str(wid)[-6:].upper()}"
        c.execute("INSERT INTO work_orders (id, asset_id, external_id, work_type, opened_at, findings, maintenance_window_id) VALUES (%s,%s,%s,%s,%s,'{}',%s)", [wid, body.asset_id, ext, body.work_type, a["sim_clock"], body.maintenance_window_id])
        if body.maintenance_window_id:
            c.execute("UPDATE maintenance_windows SET status = 'scheduled' WHERE id = %s AND asset_id = %s", [body.maintenance_window_id, body.asset_id])
        audit.record(c, ctx, "workorder.created", "work_order", wid, {"asset": a["name"], "work_type": body.work_type, "external_id": ext})
        return 201, {"id": wid, "external_id": ext, "asset": a["name"], "work_type": body.work_type, "status": "open"}
    return run(ctx, idem, body, work)


class CompleteIn(BaseModel):
    failure_mode_found: str = Field(pattern="^(" + "|".join(sim.MODES) + "|none)$")
    notes: str = Field(min_length=5, max_length=1000)


@app.post("/v1/work-orders/{wo_id}:complete", tags=["work-orders"], summary="Complete a work order: what the technician found becomes a training label, and the asset starts a new life")
def complete(wo_id: uuid.UUID, body: CompleteIn, ctx: Ctx = Depends(auth("workorders:write")), idem: str | None = IdemKey):
    def work(c):
        wo = c.execute("SELECT w.* FROM work_orders w JOIN assets a ON a.id = w.asset_id JOIN sites s ON s.id = a.site_id WHERE w.id = %s FOR UPDATE OF w", [wo_id]).fetchone()
        if not wo:
            raise Problem(404, "work_order_not_found")
        if wo["completed_at"]:
            raise Problem(409, "already_completed")
        a = get_asset(c, wo["asset_id"], lock=True)
        pred = c.execute("SELECT failure_mode, hours_p50 FROM rul_predictions WHERE asset_id = %s ORDER BY predicted_at DESC LIMIT 1", [a["id"]]).fetchone()
        c.execute("UPDATE work_orders SET completed_at = %s, findings = %s WHERE id = %s",
                  [a["sim_clock"], Jsonb({"failure_mode_found": body.failure_mode_found, "notes": body.notes, "model_said": pred and pred["failure_mode"], "technician": ctx.actor_name}), wo_id])
        c.execute("INSERT INTO model_labels (id, asset_id, label_type, failure_mode, occurred_at, source) VALUES (%s,%s,%s,%s,%s,%s)",
                  [uuid.uuid7(), a["id"], "confirmed_failure_mode" if body.failure_mode_found != "none" else "false_alarm", None if body.failure_mode_found == "none" else body.failure_mode_found,
                   a["sim_clock"], f"work_order:{wo['external_id']}"])
        if body.failure_mode_found != "none":      # the worn part was replaced: the simulated machine starts a fresh life and the baseline is re-learned
            c.execute("UPDATE assets SET overhauled_at = %s, sim = %s WHERE id = %s", [a["sim_clock"], Jsonb({**a["sim"], "age_h": 0.0, "life_h": 7500.0, "age_at_overhaul_h": 0.0}), a["id"]])
        c.execute("UPDATE alerts SET status = 'closed' WHERE asset_id = %s AND status <> 'closed'", [a["id"]])
        c.execute("UPDATE maintenance_windows SET status = 'done' WHERE id = %s", [wo["maintenance_window_id"]])
        audit.record(c, ctx, "workorder.completed", "work_order", wo_id, {"asset": a["name"], "found": body.failure_mode_found, "model_said": pred and pred["failure_mode"]})
        return 200, {"id": wo_id, "external_id": wo["external_id"], "asset": a["name"], "status": "completed", "failure_mode_found": body.failure_mode_found,
                     "model_said": pred and pred["failure_mode"], "model_was_right": bool(pred and pred["failure_mode"] == body.failure_mode_found),
                     "labels_for_asset": c.execute("SELECT count(*) AS n FROM model_labels WHERE asset_id = %s", [a["id"]]).fetchone()["n"]}
    return run(ctx, idem, body, work)
