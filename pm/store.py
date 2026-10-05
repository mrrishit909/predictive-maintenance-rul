"""stream-ingestion + historian: store waveform windows, compute features, score the asset, raise alerts.

Raw waveforms are the bulky part. They are kept for the first windows after an overhaul (the healthy reference) and
the most recent ones; older waveforms are dropped and only their features remain.
"""
import datetime
import hashlib
import uuid

import numpy as np
from psycopg.types.json import Jsonb

from core import db

from . import dsp, models, sim

KEEP_RECENT = 40


def asset_features(c, asset):
    """Feature rows of the asset's accelerometer since its last overhaul, oldest first."""
    return c.execute("""SELECT w.id, w.started_at, w.shaft_hz, f.feature_json FROM signal_windows w JOIN features f ON f.window_id = w.id JOIN sensors s ON s.id = w.sensor_id
                         WHERE s.asset_id = %s AND w.started_at >= %s ORDER BY w.started_at""", [asset["id"], asset["overhauled_at"]]).fetchall()


def ingest(c, tenant_id, asset, sensor_id, windows, bundle):
    """windows: [(started_at, shaft_hz, waveform float array)] in time order. Returns counts and the fresh assessment."""
    new_rows, feats, blobs = [], [], []
    for started, shaft, wave in windows:
        wave = np.asarray(wave, np.float32)
        raw = wave.astype(np.float16).tobytes()
        sha = hashlib.sha256(raw).hexdigest()
        wid = uuid.uuid7()
        new_rows.append((wid, sensor_id, started, int(1000 * len(wave) / sim.FS), f"blob:{sha}", sha, float(shaft)))
        feats.append((sensor_id, wid, dsp.VERSION, Jsonb(dict(zip(dsp.NAMES, dsp.features(wave, shaft))))))
        blobs.append((sha, tenant_id, raw))
    before = c.execute("SELECT count(*) AS n FROM signal_windows WHERE sensor_id = %s", [sensor_id]).fetchone()["n"]
    db.many(c, "INSERT INTO waveform_blobs VALUES (%s,%s,%s) ON CONFLICT DO NOTHING", blobs)
    db.many(c, "INSERT INTO signal_windows (id, sensor_id, started_at, duration_ms, object_uri, checksum, shaft_hz) VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (sensor_id, started_at) DO NOTHING", new_rows)
    db.many(c, "INSERT INTO features SELECT %s,%s,%s,%s WHERE EXISTS (SELECT 1 FROM signal_windows WHERE id = %s)", [(*f, f[1]) for f in feats])
    stored = c.execute("SELECT count(*) AS n FROM signal_windows WHERE sensor_id = %s", [sensor_id]).fetchone()["n"] - before
    # retention: drop waveforms that are neither part of the healthy reference nor recent
    c.execute("""DELETE FROM waveform_blobs b USING signal_windows w
                  WHERE w.sensor_id = %s AND 'blob:' || b.sha256 = w.object_uri AND w.started_at >= %s
                    AND w.id NOT IN (SELECT id FROM signal_windows WHERE sensor_id = %s AND started_at >= %s ORDER BY started_at LIMIT %s)
                    AND w.id NOT IN (SELECT id FROM signal_windows WHERE sensor_id = %s ORDER BY started_at DESC LIMIT %s)""",
              [sensor_id, asset["overhauled_at"], sensor_id, asset["overhauled_at"], models.BASE_N, sensor_id, KEEP_RECENT])
    return {"received": len(windows), "stored": stored, "duplicates_ignored": len(windows) - stored}, score_asset(c, asset, bundle, since=windows[0][0] if windows else None)


def score_asset(c, asset, bundle, since=None):
    """Re-assess the asset over everything since its overhaul; write scores and predictions for windows at or after `since`."""
    rows = asset_features(c, asset)
    if len(rows) < models.BASE_N + 1:
        return None
    ages = [(r["started_at"] - asset["overhauled_at"]).total_seconds() / 3600 + asset["sim"].get("age_at_overhaul_h", 0.0) for r in rows]
    a = models.assess(bundle, [[r["feature_json"][n] for n in dsp.NAMES] for r in rows], ages)
    mode = max(a["mode"], key=a["mode"].get)
    fresh = [i for i, r in enumerate(rows) if since is None or r["started_at"] >= since]
    c.execute("DELETE FROM anomaly_scores WHERE asset_id = %s AND scored_at >= %s", [asset["id"], rows[fresh[0]]["started_at"]] if fresh else [asset["id"], datetime.datetime.max])
    c.execute("DELETE FROM rul_predictions WHERE asset_id = %s AND predicted_at >= %s", [asset["id"], rows[fresh[0]]["started_at"]] if fresh else [asset["id"], datetime.datetime.max])
    db.many(c, "INSERT INTO anomaly_scores (id, asset_id, scored_at, score, model_version, evidence) VALUES (%s,%s,%s,%s,%s,%s)",
            [(uuid.uuid7(), asset["id"], rows[i]["started_at"], float(a["score"][i]), models.VERSION, Jsonb({"threshold": bundle["threshold"], "relative_levels": dict(zip(models.INPUTS[:models.N_REL], np.round(a["x"][i, :models.N_REL], 3).tolist()))}))
             for i in fresh])
    db.many(c, "INSERT INTO rul_predictions (id, asset_id, predicted_at, hours_p50, hours_p10, hours_p90, failure_mode, model_version) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            [(uuid.uuid7(), asset["id"], rows[i]["started_at"], float(a["p50"][i]), float(a["p10"][i]), float(a["p90"][i]), mode if a["score"][i] > bundle["threshold"] else None, models.VERSION) for i in fresh])
    if a["anomalous"] and not c.execute("SELECT 1 FROM alerts WHERE asset_id = %s AND status <> 'closed'", [asset["id"]]).fetchone():
        i = a["alarm_index"]
        c.execute("INSERT INTO alerts (id, asset_id, opened_at, severity, message) VALUES (%s,%s,%s,%s,%s)",
                  [uuid.uuid7(), asset["id"], rows[i]["started_at"], "high" if a["p50"][-1] < 200 else "medium",
                   f"{asset['name']}: vibration pattern left its healthy range ({mode.replace('_', ' ')} signature); estimated {a['p50'][-1]:.0f} h of life left"])
    return {"anomalous": a["anomalous"], "score": float(a["score"][-1]), "p50": float(a["p50"][-1]), "mode": mode}
