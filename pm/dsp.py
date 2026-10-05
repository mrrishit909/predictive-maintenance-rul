"""feature-service: detrend, window, FFT, envelope spectrum and scalar condition indicators for one waveform window."""
import numpy as np
from scipy.signal import hilbert

from .sim import BPFI, BPFO, FS, N

VERSION = "features-1.0.0"
NAMES = ["rms", "peak", "crest_factor", "kurtosis", "band_0_200", "band_200_1k", "band_1k_2k", "band_2k_4k", "order_1x", "order_2x", "env_bpfo", "env_bpfo_2x", "env_bpfi", "hf_floor"]
_hann = np.hanning(N)
_freq = np.fft.rfftfreq(N, 1 / FS)
BANDS = [(0, 200), (200, 1000), (1000, 2000), (2000, 4096)]


def spectrum(x):
    """Single-sided amplitude spectrum of a detrended, Hann-windowed window."""
    x = x - np.mean(x)
    return np.abs(np.fft.rfft(x * _hann)) * 2 / _hann.sum()


def envelope_spectrum(x, band=(1500, 3500)):
    """Demodulate the resonance band: band-pass in the frequency domain, Hilbert envelope, spectrum of the envelope.
    A bearing defect shows up here as a line at its defect frequency even when it is buried in the raw spectrum."""
    X = np.fft.rfft(x - np.mean(x))
    X[(_freq < band[0]) | (_freq > band[1])] = 0
    env = np.abs(hilbert(np.fft.irfft(X, N)))
    return spectrum(env)


def _line(spec, hz, tol=3.0):
    sel = (_freq > hz - tol) & (_freq < hz + tol)
    return float(spec[sel].max())


def features(x, shaft_hz):
    x = np.asarray(x, np.float64)
    x = x - x.mean()
    rms = float(np.sqrt(np.mean(x ** 2)))
    peak = float(np.abs(x).max())
    spec, env = spectrum(x), envelope_spectrum(x)
    return [rms, peak, peak / rms, float(np.mean(x ** 4) / rms ** 4), *[float(np.sqrt(np.sum(spec[(_freq >= lo) & (_freq < hi)] ** 2))) for lo, hi in BANDS],
            _line(spec, shaft_hz), _line(spec, 2 * shaft_hz), _line(env, BPFO * shaft_hz), _line(env, 2 * BPFO * shaft_hz), _line(env, BPFI * shaft_hz),
            float(np.median(spec[_freq > 3000]))]
