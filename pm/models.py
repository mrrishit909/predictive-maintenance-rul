"""anomaly-service, failure-mode classifier and rul-service, trained on simulated run-to-failure histories.

Every indicator is taken relative to the asset's own healthy baseline (its first windows after installation or overhaul),
because absolute vibration levels differ from machine to machine. Remaining useful life (RUL) is predicted as three
quantiles, and the 10-90% band is widened by conformal calibration on assets the model did not train on.
"""
import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor, IsolationForest

from . import dsp, sim

VERSION = "pdm-1.0.0"
KEY = [dsp.NAMES.index(n) for n in ("rms", "order_1x", "order_2x", "env_bpfo", "hf_floor")]
BASE_N, TREND_N, RUL_CAP = 10, 8, 600.0
INPUTS = [f"rel_{dsp.NAMES[k]}" for k in KEY] + [f"trend_{dsp.NAMES[k]}" for k in KEY] + ["crest_factor", "kurtosis", "age_h"]
N_REL = len(KEY)


def derive(feats, ages):
    """Model inputs for each window of one asset since its last overhaul: level relative to baseline, recent trend, shape, age."""
    f = np.asarray(feats, float)
    base = np.median(f[:BASE_N, KEY], axis=0)
    rel = np.log(f[:, KEY] / base)
    trend = np.zeros_like(rel)
    for i in range(1, len(f)):
        lo = max(0, i - TREND_N + 1)
        if i - lo >= 2:
            x = np.asarray(ages[lo:i + 1], float)
            trend[i] = np.polyfit(x - x.mean(), rel[lo:i + 1], 1)[0] * 100          # change in log level per 100 hours
    return np.column_stack([rel, trend, f[:, dsp.NAMES.index("crest_factor")], f[:, dsp.NAMES.index("kurtosis")], ages])


def history(rng, n_assets):
    """Simulate a fleet to failure -> list of {mode, life, x (inputs), rul, frac (share of life used)}."""
    out = []
    for mode, life, shaft in sim.fleet(rng, n_assets):
        ages, speeds, waves = sim.run_to_failure(rng, mode, life, shaft)
        feats = [dsp.features(w, s) for w, s in zip(waves, speeds)]
        out.append({"mode": mode, "life": life, "x": derive(feats, ages), "rul": life - ages, "frac": ages / life, "rms_rel": np.log(np.array(feats)[:, 0] / np.median(np.array(feats)[:BASE_N, 0]))})
    return out


def distance(stat, x):
    """Mahalanobis distance of the relative indicators from the healthy cloud: how many 'standard deviations' out, all indicators at once."""
    d = np.asarray(x)[:, :N_REL] - stat["mu"]
    return np.sqrt(np.einsum("ij,jk,ik->i", d, stat["icov"], d))


def _stack(assets, key):
    return np.concatenate([a[key] for a in assets])


def first_alarm(flags, need=2):
    """Index of the first window where `need` consecutive windows are flagged, or None."""
    run = 0
    for i, f in enumerate(flags):
        run = run + 1 if f else 0
        if run >= need:
            return i - need + 1
    return None


def train(seed=0, n_assets=150):
    """-> (bundle, metrics). 60% of assets train, 20% calibrate the interval and thresholds, 20% test."""
    rng = np.random.default_rng(seed)
    assets = history(rng, n_assets)
    a, b = int(0.6 * n_assets), int(0.8 * n_assets)
    tr, cal, te = assets[:a], assets[a:b], assets[b:]
    healthy = lambda s: np.concatenate([x["x"][(x["frac"] < 0.25) & (np.arange(len(x["frac"])) >= BASE_N), :N_REL] for x in s])      # noqa: E731
    h_tr, h_cal = healthy(tr), healthy(cal)
    mu, icov = h_tr.mean(axis=0), np.linalg.inv(np.cov(h_tr.T))
    stat = {"mu": mu, "icov": icov}
    score = lambda x: distance(stat, x)      # noqa: E731
    # every alarm is set to the same false-alarm rate on healthy calibration windows, so lead times are comparable
    thr = float(np.quantile(distance(stat, h_cal), 0.995))
    iforest = IsolationForest(n_estimators=200, random_state=0).fit(h_tr)
    if_thr = float(np.quantile(-iforest.score_samples(h_cal), 0.995))
    rms_thr = float(np.quantile(np.concatenate([x["rms_rel"][(x["frac"] < 0.25) & (np.arange(len(x["frac"])) >= BASE_N)] for x in cal]), 0.995))
    y = lambda s: np.minimum(_stack(s, "rul"), RUL_CAP)      # noqa: E731
    q = {p: HistGradientBoostingRegressor(loss="quantile", quantile=p, max_iter=250, learning_rate=0.06, random_state=0).fit(_stack(tr, "x"), y(tr)) for p in (0.1, 0.5, 0.9)}
    live = _stack(cal, "rul") < RUL_CAP                                # calibrate where the target is not pinned at the cap
    lo, hi, yc = q[0.1].predict(_stack(cal, "x"))[live], q[0.9].predict(_stack(cal, "x"))[live], y(cal)[live]
    conf = float(np.quantile(np.maximum(lo - yc, yc - hi), min(1.0, 0.8 * (1 + 1 / len(lo)))))      # conformalised quantile regression: widen (or narrow) the band by this many hours
    late = lambda s: _stack(s, "frac") > 0.6      # noqa: E731
    modes = np.concatenate([[x["mode"]] * len(x["rul"]) for x in tr])
    clf = HistGradientBoostingClassifier(max_iter=150, random_state=0).fit(_stack(tr, "x")[late(tr)][:, :-1], modes[late(tr)])
    bundle = {"stat": stat, "threshold": thr, "q": q, "conformal_h": conf, "mode": clf, "mean_life": float(np.mean([x["life"] for x in tr]))}

    # ---- held-out assets ----------------------------------------------------------------------------------
    xt, yt = _stack(te, "x"), y(te)
    p10, p50, p90 = (q[p].predict(xt) for p in (0.1, 0.5, 0.9))
    act = _stack(te, "rul") <= 300                                    # the last 300 hours, where a wrong RUL costs money
    naive = np.clip(bundle["mean_life"] - xt[:, -1], 0, RUL_CAP)
    alarms = {"distance": (lambda x: score(x["x"]), thr), "isolation_forest": (lambda x: -iforest.score_samples(x["x"][:, :N_REL]), if_thr), "rms": (lambda x: x["rms_rel"], rms_thr)}
    lead = {k: [] for k in alarms}
    early = {k: 0 for k in alarms}
    for x in te:
        ok = np.arange(len(x["rul"])) >= BASE_N
        for k, (fn, t) in alarms.items():
            i = first_alarm((fn(x) > t) & ok)
            lead[k].append(float(x["rul"][i]) if i is not None else 0.0)        # never alarmed = zero warning
            early[k] += int(i is not None and x["frac"][i] < 0.25)
    te_modes = np.concatenate([[x["mode"]] * len(x["rul"]) for x in te])
    pred_mode = clf.predict(xt[late(te)][:, :-1])
    by_mode = {m: {k: round(float(np.median([v for v, x in zip(lead[k], te) if x["mode"] == m]))) for k in alarms} for m in sim.MODES}
    unc = _stack(te, "rul") < RUL_CAP
    metrics = {"version": VERSION, "assets": {"train": len(tr), "calibrate": len(cal), "test": len(te)}, "windows": {"train": int(len(_stack(tr, "rul"))), "test": int(len(yt))},
               "rul": {"mae_h_last_300h": round(float(np.mean(np.abs(p50[act] - yt[act]))), 1), "baseline_mean_life_minus_age_mae_h": round(float(np.mean(np.abs(naive[act] - yt[act]))), 1),
                       "coverage_raw_10_90": round(float(np.mean(((yt >= p10) & (yt <= p90))[unc])), 3), "coverage_conformal": round(float(np.mean(((yt >= p10 - conf) & (yt <= p90 + conf))[unc])), 3),
                       "target_coverage": 0.8, "conformal_adjustment_h": round(conf, 1), "mean_band_width_h_last_300h": round(float(np.mean((p90 - p10 + 2 * conf)[act])), 1)},
               "anomaly": {"false_alarm_rate_target": 0.005, "median_warning_h": {k: round(float(np.median(v))) for k, v in lead.items()},
                           "assets_never_alarmed": {k: int(sum(v == 0 for v in vs)) for k, vs in lead.items()}, "assets_alarmed_while_healthy": early, "median_warning_h_by_mode": by_mode},
               "mode": {"accuracy_last_40pct_of_life": round(float(np.mean(pred_mode == te_modes[late(te)])), 3), "baseline_majority": 0.25}}
    return bundle, metrics


def assess(bundle, feats, ages):
    """Current state of one asset from its window features since overhaul: anomaly series, RUL quantiles, likely failure mode."""
    x = derive(feats, ages)
    s = distance(bundle["stat"], x)
    q10, q50, q90 = (np.clip(bundle["q"][p].predict(x), 0, None) for p in (0.1, 0.5, 0.9))
    c = bundle["conformal_h"]
    lo, hi = np.clip(np.minimum(q10, q50) - c, 0, None), np.maximum(q90, q50) + c
    proba = bundle["mode"].predict_proba(x[-1:, :-1])[0]
    alarm = first_alarm((s > bundle["threshold"]) & (np.arange(len(s)) >= BASE_N))
    return {"x": x, "score": s, "p10": lo, "p50": q50, "p90": hi, "mode": dict(zip(bundle["mode"].classes_, proba)), "alarm_index": alarm, "anomalous": bool(alarm is not None and s[-1] > bundle["threshold"])}
