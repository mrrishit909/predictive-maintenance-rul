"""Train the models on a simulated fleet, then load one plant with twelve monitored machines.

    python -m pm.seed [--if-empty]
"""
import datetime
import hashlib
import pickle
import sys
import uuid

import numpy as np
from psycopg.types.json import Jsonb

from core import db

from . import models, sim, store

TENANTS = {"acme": "Acme Packaging", "other": "Second Tenant Ltd"}
ROLES = ["technician", "planner", "admin"]
ECON = {"default": {"downtime_cost_per_h": 1200, "part_cost": 1500, "labor_cost": 800, "repair_h": 4, "unplanned_h": 16, "part_lead_h": 24}}
# name, type, failure mode, life (h), shaft speed (Hz), age at seed (h), economics overrides
FLEET = [("Line 2 conveyor drive motor M-207", "motor", "outer_race", 900, 27.4, 180, {"downtime_cost_per_h": 4000, "part_cost": 1800, "labor_cost": 900, "unplanned_h": 18, "part_lead_h": 48}),
         ("Dust extraction fan F-11", "fan", "imbalance", 760, 24.6, 190, {"downtime_cost_per_h": 1500, "part_cost": 600, "part_lead_h": 0}),
         ("Chilled water pump P-03", "pump", "misalignment", 1250, 29.1, 400, {"downtime_cost_per_h": 2500, "part_cost": 2200}),
         ("Line 1 conveyor drive motor M-104", "motor", "outer_race", 7000, 26.8, 320, {"downtime_cost_per_h": 4000}), ("Line 3 gearbox G-31", "gearbox", "lubrication", 8200, 25.5, 510, {"downtime_cost_per_h": 3500}),
         ("Compressor C-02", "compressor", "imbalance", 9000, 28.3, 150, {}), ("Cooling tower fan F-21", "fan", "imbalance", 6800, 24.2, 260, {}), ("Palletiser spindle S-08", "spindle", "outer_race", 7600, 29.6, 90, {}),
         ("Feed pump P-12", "pump", "misalignment", 8800, 27.9, 420, {}), ("Wrapper drive motor M-311", "motor", "lubrication", 7300, 25.1, 600, {}),
         ("Vacuum blower B-05", "blower", "outer_race", 9500, 26.2, 240, {}), ("Hydraulic power unit H-01", "pump", "lubrication", 6400, 28.8, 330, {})]


def token(tenant, role):
    """Demo credentials for local use only."""
    return f"{tenant}-{role}-demo"


def asset_rng(asset_id, age):
    return np.random.default_rng([asset_id.int % 2**32, int(age)])


def main(if_empty=False):
    db.migrate()
    with db.tx() as c:
        if if_empty and c.execute("SELECT 1 FROM tenants LIMIT 1").fetchone():
            return print("already seeded")
    bundle, metrics = models.train()
    now = datetime.datetime.now(datetime.UTC).replace(minute=0, second=0, microsecond=0)
    out = {"metrics": metrics}
    with db.tx() as c:
        c.execute("INSERT INTO model_registry (name, version, data_snapshot, metrics, baseline, artifact) VALUES ('condition-models',%s,%s,%s,%s,%s)",
                  [models.VERSION, "simulated fleet, seed 0, 150 run-to-failure assets", Jsonb(metrics), Jsonb({"rul": "mean life minus age", "anomaly": "RMS alarm at the same false-alarm rate; isolation forest"}), pickle.dumps(bundle)])
        for key, name in TENANTS.items():
            t = uuid.uuid5(uuid.NAMESPACE_DNS, f"{key}.predictive-maintenance.example")
            c.execute("INSERT INTO tenants (id, name) VALUES (%s,%s)", [t, name])
            for role in ROLES:
                c.execute("INSERT INTO api_tokens VALUES (%s,%s,%s,%s,%s)", [hashlib.sha256(token(key, role).encode()).hexdigest(), t, uuid.uuid5(t, role), f"{role} ({name})", role])
            site = uuid.uuid5(t, "site")
            c.execute("INSERT INTO sites VALUES (%s,%s,%s,'UTC',%s)", [site, t, "Riverside Plant" if key == "acme" else "Second Plant", now])
            for name_, kind, mode, life, shaft, age, econ in (FLEET if key == "acme" else FLEET[3:5]):
                aid = uuid.uuid5(site, name_)
                born = now - datetime.timedelta(hours=age)
                asset = {"id": aid, "name": name_, "overhauled_at": born, "sim": {"mode": mode, "life_h": life, "shaft_hz": shaft, "age_h": age, "age_at_overhaul_h": 0.0}}
                c.execute("INSERT INTO assets (id, site_id, asset_type, manufacturer, model, criticality, commissioned_on, name, overhauled_at, economics, sim) VALUES (%s,%s,%s,'Synthetic Drives','SD-200',%s,%s,%s,%s,%s,%s)",
                          [aid, site, kind, 5 if econ.get("downtime_cost_per_h", 0) >= 3500 else 3, born.date(), name_, born, Jsonb({**ECON["default"], **econ}), Jsonb(asset["sim"])])
                sid = uuid.uuid5(aid, "accelerometer")
                c.execute("INSERT INTO sensors VALUES (%s,%s,'accelerometer','g',%s,'radial',%s)", [sid, aid, sim.FS, Jsonb({"window_samples": sim.N})])
                ages, speeds, waves = sim.run_to_failure(asset_rng(aid, 0), mode, life, shaft, until_h=age)
                store.ingest(c, t, asset, sid, [(born + datetime.timedelta(hours=float(a)), s, w) for a, s, w in zip(ages, speeds, waves)], bundle)
            out[key] = {"tenant": t, "site": site}
    print(f"trained on {metrics['assets']['train']} simulated run-to-failure assets; plant seeded with {len(FLEET)} machines")
    print("demo tokens:", ", ".join(token(t, r) for t in TENANTS for r in ROLES))
    return out


if __name__ == "__main__":
    main("--if-empty" in sys.argv)
