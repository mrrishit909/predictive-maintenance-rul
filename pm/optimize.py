"""maintenance-optimizer: put each at-risk asset into a maintenance window (or leave it) to minimise expected cost.

Cost of servicing asset a in window w = P(it fails first) x unplanned-failure cost
                                      + P(it survives) x (planned work + lost production unless the line is already stopped)
                                      + value of the bearing life thrown away by going early.
Constraints: one choice per asset, technician capacity per window, and the spare part must have arrived.
"""
import math

from ortools.sat.python import cp_model

VERSION = "maint-cpsat-1.0.0"
Z80 = 1.2816


def fail_by(t, p10, p50, p90):
    """P(failure before t hours) from the RUL quantiles, as a log-normal."""
    if t <= 0:
        return 0.0
    mu, sigma = math.log(max(p50, 1.0)), max(0.15, (math.log(max(p90, 2.0)) - math.log(max(p10, 1.0))) / (2 * Z80))
    return 0.5 * (1 + math.erf((math.log(t) - mu) / (sigma * math.sqrt(2))))


def option_cost(a, w):
    """Expected cost (and its parts) of doing asset a in window w; w=None means not in this horizon."""
    unplanned = a["unplanned_h"] * a["downtime_cost_per_h"] + 2.5 * a["part_cost"] + 1.5 * a["labor_cost"]
    if w is None:
        p = fail_by(a["horizon_h"], a["p10"], a["p50"], a["p90"])
        return {"expected": p * unplanned, "p_fail_before": p, "failure": p * unplanned, "planned": 0.0, "life_wasted": 0.0}
    p = fail_by(w["start_h"], a["p10"], a["p50"], a["p90"])
    planned = a["part_cost"] + a["labor_cost"] + (0 if w["production_stopped"] else a["repair_h"] * a["downtime_cost_per_h"])
    wasted = a["part_cost"] * max(0.0, a["p50"] - w["start_h"]) / a["mean_life_h"]
    return {"expected": p * unplanned + (1 - p) * (planned + wasted), "p_fail_before": p, "failure": p * unplanned, "planned": (1 - p) * planned, "life_wasted": (1 - p) * wasted}


def solve(assets, windows):
    """-> {schedule: [{asset, window or None, cost parts}], total, solver}. Exact (CP-SAT) over asset x window choices."""
    m = cp_model.CpModel()
    x, cost = {}, {}
    for i, a in enumerate(assets):
        opts = [None] + [j for j, w in enumerate(windows) if w["start_h"] >= a["part_lead_h"]]
        for j in opts:
            x[i, j] = m.NewBoolVar(f"x{i}_{j}")
            cost[i, j] = option_cost(a, None if j is None else windows[j])
        m.AddExactlyOne(x[i, j] for j in opts)
    for j, w in enumerate(windows):
        m.Add(sum(v for (i, jj), v in x.items() if jj == j) <= w["technicians"])
    m.Minimize(sum(int(round(cost[k]["expected"] * 100)) * v for k, v in x.items()))
    s = cp_model.CpSolver()
    s.parameters.max_time_in_seconds, s.parameters.num_workers, s.parameters.random_seed = 10, 1, 0
    status = s.Solve(m)
    chosen = {i: j for (i, j), v in x.items() if s.Value(v)}
    return _report(assets, windows, chosen, cost, s.StatusName(status))


def _report(assets, windows, chosen, cost, status):
    rows = [{"asset": assets[i]["name"], "asset_id": assets[i]["id"], "window": None if j is None else j, **{k: round(v, 2) for k, v in cost[i, j].items()}} for i, j in sorted(chosen.items())]
    return {"schedule": rows, "total_expected_cost": round(sum(r["expected"] for r in rows), 2), "solver": {"name": "OR-Tools CP-SAT", "status": status.lower(), "version": VERSION}}


def baseline(assets, windows, policy):
    """run_to_failure: nothing is scheduled. earliest: every asset that is alarming takes the first window it can get."""
    cost, chosen, used = {}, {}, [0] * len(windows)
    for i, a in enumerate(assets):
        j = None
        if policy == "earliest" and a["alarming"]:
            j = next((k for k, w in enumerate(windows) if w["start_h"] >= a["part_lead_h"] and used[k] < w["technicians"]), None)
            if j is not None:
                used[j] += 1
        chosen[i] = j
        cost[i, j] = option_cost(a, None if j is None else windows[j])
    return _report(assets, windows, chosen, cost, policy)
