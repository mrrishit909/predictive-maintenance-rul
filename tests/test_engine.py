"""Unit tests: simulator, signal processing, derived inputs, failure probability and the maintenance optimizer."""
import numpy as np
import pytest

from pm import dsp, models, optimize, sim


def test_fft_finds_a_known_tone_and_its_amplitude():
    t = np.arange(sim.N) / sim.FS
    spec = dsp.spectrum(0.5 * np.sin(2 * np.pi * 120 * t) + 3.0)            # the constant offset is removed by detrending
    freq = np.fft.rfftfreq(sim.N, 1 / sim.FS)
    assert freq[spec.argmax()] == 120 and spec.max() == pytest.approx(0.5, rel=0.02) and spec[0] < 1e-6


def test_envelope_spectrum_reveals_a_buried_bearing_defect():
    rng = np.random.default_rng(1)
    shaft = 27.0
    healthy, worn = sim.window(rng, shaft, 0.0, "outer_race"), sim.window(rng, shaft, 0.5, "outer_race")
    f0, f1 = dict(zip(dsp.NAMES, dsp.features(healthy, shaft))), dict(zip(dsp.NAMES, dsp.features(worn, shaft)))
    assert f1["env_bpfo"] > 15 * f0["env_bpfo"]                              # the defect line stands out in the demodulated band...
    assert f1["rms"] < 4 * f0["rms"] and f1["kurtosis"] > f0["kurtosis"] + 2  # ...long before the overall level says much
    env = dsp.envelope_spectrum(worn)
    freq = np.fft.rfftfreq(sim.N, 1 / sim.FS)
    near = (freq > 50) & (freq < 300)
    assert abs(freq[near][env[near].argmax()] - sim.BPFO * shaft) < 4


@pytest.mark.parametrize("mode,indicator", [("imbalance", "order_1x"), ("misalignment", "order_2x"), ("outer_race", "env_bpfo"), ("lubrication", "hf_floor")])
def test_each_failure_mode_moves_its_own_indicator(mode, indicator):
    rng = np.random.default_rng(2)
    a = np.mean([dsp.features(sim.window(rng, 26.0, 0.0, mode), 26.0) for _ in range(8)], axis=0)
    b = np.mean([dsp.features(sim.window(rng, 26.0, 0.8, mode), 26.0) for _ in range(8)], axis=0)
    growth = dict(zip(dsp.NAMES, b / a))
    assert growth[indicator] == max(growth[k] for k in ("order_1x", "order_2x", "env_bpfo", "hf_floor")) and growth[indicator] > 2.5


def test_degradation_and_run_to_failure_shapes():
    assert sim.degradation(0, 900) == 0 and sim.degradation(900, 900) == 1 and sim.degradation(450, 900) == pytest.approx(0.125)
    ages, speeds, waves = sim.run_to_failure(np.random.default_rng(0), "imbalance", 120, 25.0, until_h=60, start_h=24)
    assert ages.tolist() == [24, 30, 36, 42, 48, 54] and waves.shape == (6, sim.N) and waves.dtype == np.float32


def test_inputs_are_relative_to_the_machines_own_baseline():
    rng = np.random.default_rng(3)
    ages, speeds, waves = sim.run_to_failure(rng, "imbalance", 600, 25.0, until_h=300)
    feats = np.array([dsp.features(w, s) for w, s in zip(waves, speeds)])
    x1, x2 = models.derive(feats, ages), models.derive(feats * 7.0, ages)       # a sensor with seven times the gain
    assert np.allclose(x1[:, :models.N_REL], x2[:, :models.N_REL]) and np.allclose(x1[:models.BASE_N, :models.N_REL].mean(0), 0, atol=0.25)
    assert models.first_alarm([0, 1, 0, 1, 1, 1]) == 3 and models.first_alarm([0, 1, 0]) is None


def test_failure_probability_from_quantiles():
    assert optimize.fail_by(0, 100, 200, 400) == 0
    assert optimize.fail_by(200, 100, 200, 400) == pytest.approx(0.5)
    assert optimize.fail_by(100, 100, 200, 400) == pytest.approx(0.1, abs=0.005) and optimize.fail_by(400, 100, 200, 400) == pytest.approx(0.9, abs=0.005)
    p = [optimize.fail_by(t, 100, 200, 400) for t in range(0, 800, 40)]
    assert p == sorted(p)


A = {"id": "a", "name": "motor", "p10": 200, "p50": 260, "p90": 380, "horizon_h": 336, "mean_life_h": 1000, "alarming": True, "downtime_cost_per_h": 4000, "part_cost": 1800, "labor_cost": 900,
     "repair_h": 4, "unplanned_h": 18, "part_lead_h": 48}
W = [{"start_h": 6 + 24 * d, "production_stopped": d in (4, 5), "technicians": 1} for d in range(14)]


def test_optimizer_waits_for_the_planned_stop_when_risk_allows():
    out = optimize.solve([A], W)
    (row,) = out["schedule"]
    assert W[row["window"]]["production_stopped"] and out["solver"]["status"] == "optimal"
    assert row["expected"] == pytest.approx(row["failure"] + row["planned"] + row["life_wasted"], abs=0.02)
    assert out["total_expected_cost"] < optimize.baseline([A], W, "earliest")["total_expected_cost"] < optimize.baseline([A], W, "run_to_failure")["total_expected_cost"]
    assert optimize.baseline([A], W, "earliest")["schedule"][0]["window"] == 2      # the part takes 48 h to arrive: day 3 is the first opening


def test_optimizer_goes_early_for_an_urgent_machine_and_respects_capacity_and_lead_time():
    urgent = {**A, "id": "b", "name": "fan", "p10": 40, "p50": 70, "p90": 120, "part_lead_h": 0, "downtime_cost_per_h": 1500}
    rows = {r["asset"]: r for r in optimize.solve([A, urgent], W)["schedule"]}
    assert W[rows["fan"]["window"]]["start_h"] < 60 and not W[rows["fan"]["window"]]["production_stopped"]       # too risky to wait four days
    twin = {**A, "id": "c", "name": "motor 2"}
    rows = optimize.solve([A, twin], W)["schedule"]
    assert rows[0]["window"] != rows[1]["window"]                                                              # one technician per slot
    late_part = {**A, "part_lead_h": 200}
    assert W[optimize.solve([late_part], W)["schedule"][0]["window"]]["start_h"] >= 200
    healthy = {**A, "p10": 3000, "p50": 5000, "p90": 8000}
    assert optimize.solve([healthy], W)["schedule"][0]["window"] is None                                       # nothing to gain by touching it
