"""Integration, end-to-end and security tests against a real Postgres."""
import base64
import datetime

import numpy as np
import pytest

from core import db, jobs, scenario
from pm import sim

from .conftest import bearer

TECH, PLAN, ADMIN, OTHER = bearer("acme", "technician"), bearer("acme", "planner"), bearer("acme", "admin"), bearer("other", "technician")


def test_models_meet_their_acceptance_thresholds(seeded):
    m = seeded["metrics"]
    assert m["rul"]["mae_h_last_300h"] < 0.3 * m["rul"]["baseline_mean_life_minus_age_mae_h"]
    assert 0.74 <= m["rul"]["coverage_conformal"] <= 0.9
    assert m["anomaly"]["median_warning_h"]["distance"] > m["anomaly"]["median_warning_h"]["rms"] and m["anomaly"]["assets_never_alarmed"]["distance"] == 0
    assert m["mode"]["accuracy_last_40pct_of_life"] > 0.9


def test_signal_upload_is_validated_idempotent_and_tenant_scoped(client, seeded):
    fleet = client.get("/v1/assets", headers=TECH).json()["items"]
    assert len(fleet) == 12 and not any(a["anomalous"] for a in fleet)                # the plant starts healthy
    a = next(x for x in fleet if x["name"].startswith("Compressor"))
    wave = sim.window(np.random.default_rng(0), 28.3, 0.0, "imbalance")
    w = {"started_at": (datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1)).isoformat(), "shaft_hz": 28.3, "samples_base64": base64.b64encode(wave.astype("<f4").tobytes()).decode()}
    r = client.post("/v1/signals:batch", headers=TECH, json={"sensor_id": a["sensor_id"], "windows": [w]})
    assert r.status_code == 201 and r.json()["stored"] == 1 and r.json()["state"]["anomalous"] is False
    assert client.post("/v1/signals:batch", headers=TECH, json={"sensor_id": a["sensor_id"], "windows": [w]}).json() | {"state": None} == {"received": 1, "stored": 0, "duplicates_ignored": 1, "asset": a["name"], "state": None}
    assert client.post("/v1/signals:batch", headers=OTHER, json={"sensor_id": a["sensor_id"], "windows": [w]}).status_code == 404
    short = {**w, "samples_base64": base64.b64encode(wave[:100].astype("<f4").tobytes()).decode()}
    assert client.post("/v1/signals:batch", headers=TECH, json={"sensor_id": a["sensor_id"], "windows": [short]}).json()["code"] == "invalid_waveform"
    nan = {**w, "samples_base64": base64.b64encode(np.full(sim.N, np.nan, "<f4").tobytes()).decode()}
    assert client.post("/v1/signals:batch", headers=TECH, json={"sensor_id": a["sensor_id"], "windows": [nan]}).json()["code"] == "invalid_waveform"


@pytest.fixture(scope="module")
def demo(client):
    results, _ = scenario.run(client, drain=jobs.drain)
    return results


def test_demo_journey_end_to_end(demo):
    assert demo["run"]["result"]["windows_stored"] == 12 * 80 and demo["run"]["result"]["failed_in_service"] == []
    sp = demo["spec"]
    assert sp["bpfo_growth"] > 10 and abs(sp["windows"][1]["bpfo_hz"] - 3.58 * sp["windows"][1]["shaft_hz"]) < 0.2 and len(sp["windows"][1]["envelope"]["hz"]) > 100
    h = demo["health"]
    assert h["anomalous"] and h["score"] > 3 * h["threshold"] and h["alerts"][0]["status"] == "open" and h["series"]["score"][0] < h["threshold"]
    assert demo["ack"]["status"] == "acknowledged"
    r = demo["rul"]
    assert r["hours_p10"] <= r["hours_p50"] <= r["hours_p90"] and r["failure_mode"] == "outer_race"
    assert r["hours_p10"] - 40 <= 240 <= r["hours_p90"] + 40                           # the simulator's true remaining life is 240 h
    assert r["if_ignored"]["probability_of_failure_within_days"]["14"] > 0.5 and r["if_ignored"]["expected_loss_14_days"] > 10 * r["if_ignored"]["planned_repair_cost_line_stopped"]
    at_risk = [a for a in demo["fleet"]["items"] if a["anomalous"]]
    assert {a["failure_mode"] for a in at_risk} == {"outer_race", "imbalance", "misalignment"} and demo["fleet"]["items"][0]["name"].startswith("Dust extraction fan")
    p = demo["plan"]
    rows = {x["asset"]: x for x in p["schedule"]}
    assert rows["Line 2 conveyor drive motor M-207"]["window"]["production_stopped"] and rows["Line 2 conveyor drive motor M-207"]["window"]["start_h"] >= 48
    assert not rows["Dust extraction fan F-11"]["window"]["production_stopped"]         # the fan cannot wait for the changeover
    cost = {c["policy"]: c["total_expected_cost"] for c in p["comparison"]}
    assert cost["optimized"] < cost["fix_at_first_opening"] < cost["run_to_failure"] and p["solver"]["status"] == "optimal"
    assert demo["done"]["model_was_right"] and demo["done"]["labels_for_asset"] == 1
    h2 = demo["health2"]
    assert not h2["anomalous"] and h2["hours_since_overhaul"] < 80 and h2["alerts"][0]["status"] == "closed"
    assert demo["audit"]["chain_valid"] and {"plant.simulated", "alert.acknowledged", "maintenance.planned", "workorder.created", "workorder.completed"} <= {e["action"] for e in demo["audit"]["events"]}


def test_retention_keeps_reference_and_recent_waveforms_only(demo):
    with db.tx() as c:
        r = c.execute("""SELECT count(*) AS windows, count(b.sha256) AS with_waveform, count(f.window_id) AS with_features
                           FROM signal_windows w JOIN sensors s ON s.id = w.sensor_id JOIN assets a ON a.id = s.asset_id
                           LEFT JOIN waveform_blobs b ON 'blob:' || b.sha256 = w.object_uri LEFT JOIN features f ON f.window_id = w.id
                          WHERE a.name = 'Chilled water pump P-03'""").fetchone()
    assert r["windows"] > 140 and r["with_features"] == r["windows"] and 40 <= r["with_waveform"] <= 52


def test_work_order_rules_and_false_alarm_label(client, demo):
    fan = next(a for a in demo["fleet"]["items"] if a["name"].startswith("Dust extraction fan"))
    wo = client.post("/v1/work-orders", headers=PLAN, json={"asset_id": fan["id"], "work_type": "inspection"}).json()
    done = client.post(f"/v1/work-orders/{wo['id']}:complete", headers=TECH, json={"failure_mode_found": "none", "notes": "Inspected, nothing found yet."}).json()
    assert done["model_was_right"] is False and done["status"] == "completed"
    assert client.post(f"/v1/work-orders/{wo['id']}:complete", headers=TECH, json={"failure_mode_found": "none", "notes": "again please"}).json()["code"] == "already_completed"
    with db.tx() as c:
        assert c.execute("SELECT label_type FROM model_labels WHERE asset_id = %s", [fan["id"]]).fetchone()["label_type"] == "false_alarm"
    assert client.post("/v1/work-orders", headers=PLAN, json={"asset_id": fan["id"], "work_type": "repaint"}).status_code == 422


@pytest.mark.parametrize("headers,method,path,body,expected", [
    ({}, "GET", "/v1/assets", None, 401),
    (TECH, "POST", "/v1/maintenance:optimize", {}, 403),                             # technicians do not plan the fleet
    (PLAN, "POST", "/v1/signals:batch", {"sensor_id": "00000000-0000-0000-0000-000000000000", "windows": []}, 403),
    (PLAN, "GET", "/v1/audit", None, 403),
    (ADMIN, "GET", "/v1/audit", None, 200),
])
def test_authorization_matrix(client, headers, method, path, body, expected):
    assert client.request(method, path, headers=headers, json=body).status_code == expected


def test_tenant_isolation(client, demo):
    aid = demo["a"]["items"][0]["id"]
    for path in ("health", "spectrum", "rul"):
        assert client.get(f"/v1/assets/{aid}/{path}", headers=OTHER).status_code == 404
    assert client.post("/v1/work-orders", headers=OTHER, json={"asset_id": aid, "work_type": "inspection"}).status_code == 404
    assert client.post(f"/v1/alerts/{demo['health']['alerts'][0]['id']}:ack", headers=OTHER).status_code == 404
    assert len(client.get("/v1/assets", headers=OTHER).json()["items"]) == 2
