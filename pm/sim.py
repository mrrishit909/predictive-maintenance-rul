"""Rotating-machinery simulator: vibration waveforms for a motor-driven bearing housing as it wears out.

A window is 0.5 s of acceleration at 8,192 Hz. The signal is shaft-rate tones, a train of bearing impacts ringing a
structural resonance, and noise; one of four failure modes grows with a degradation level d in [0, 1]. Run-to-failure
histories for a fleet give the training data; one demo asset is streamed through the API.

Everything is synthetic. Defect frequencies follow textbook bearing kinematics for an invented bearing.
"""
import numpy as np

FS, N = 8192, 4096
MODES = ["outer_race", "imbalance", "misalignment", "lubrication"]
BPFO, BPFI = 3.58, 5.42            # outer / inner race defect frequency, as multiples of shaft speed
F_RES, TAU = 2400.0, 0.0015        # resonance the impacts excite (Hz) and its decay (s)
STEP_H = 6.0                       # hours between windows
_t = np.arange(N) / FS
_kernel = np.exp(-np.arange(64) / FS / TAU) * np.sin(2 * np.pi * F_RES * np.arange(64) / FS)


def degradation(age_h, life_h):
    """Wear accelerates: slow for most of the life, steep at the end."""
    return np.clip(age_h / life_h, 0, 1) ** 3


def window(rng, shaft_hz, d, mode):
    """One acceleration window (g) at degradation d for the asset's failure mode."""
    load = float(np.exp(rng.normal(0, 0.15)))                           # the machine is not always doing the same work: shaft-rate tones move with load
    a1 = 0.10 * load * (1 + (7 * d if mode == "imbalance" else 0.5 * d if mode == "misalignment" else 0))
    a2 = 0.04 * load * (1 + (9 * d if mode == "misalignment" else 0))
    x = a1 * np.sin(2 * np.pi * shaft_hz * _t + rng.uniform(0, 6.28)) + a2 * np.sin(2 * np.pi * 2 * shaft_hz * _t + rng.uniform(0, 6.28))
    amp = 0.03 + (2.2 * d if mode == "outer_race" else 0)              # impacts each time a ball passes the defect
    period = FS / (BPFO * shaft_hz)
    hits = (rng.uniform(0, period) + period * np.arange(int(N / period) + 1) * (1 + rng.normal(0, 0.01, int(N / period) + 1))).astype(int)
    train = np.zeros(N)
    hits = hits[hits < N]
    train[hits] = amp * (1 + rng.normal(0, 0.15, len(hits)))
    x += np.convolve(train, _kernel)[:N]
    noise = rng.normal(0, 0.05 * (0.7 + 0.3 * load), N)
    if mode == "lubrication":                                           # dry running: broadband high-frequency hash
        noise += np.diff(rng.normal(0, 0.05 * 6 * d, N + 1))
    return (x + noise).astype(np.float32)


def run_to_failure(rng, mode, life_h, shaft_hz, until_h=None, start_h=0.0):
    """Windows every STEP_H hours from start_h to failure (or until_h). -> (ages, shaft speeds, waveforms)."""
    ages = np.arange(start_h, until_h if until_h is not None else life_h, STEP_H)
    speeds = shaft_hz * (1 + rng.normal(0, 0.004, len(ages)))
    waves = np.stack([window(rng, s, degradation(a, life_h), mode) for a, s in zip(ages, speeds)]) if len(ages) else np.zeros((0, N), np.float32)
    return ages, speeds, waves


def fleet(rng, n):
    """Random assets: (mode, life in hours, shaft speed in Hz)."""
    return [(MODES[i % 4], float(rng.weibull(3.0) * 900 + 350), float(rng.uniform(24, 30))) for i in range(n)]
