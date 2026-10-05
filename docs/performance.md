# Performance

Docker stack (one uvicorn process, one worker, Postgres 16) after the demo: 12 machines, about 1,860 waveform windows
(4,096 samples each), features for every window and raw waveforms for the reference and recent ones.
Reproduce with `make demo`, then `make load-test ASSET=<demo asset id>`.

## Reads: fleet list, health series, remaining-life view, spectrum of the latest window (in rotation), closed loop, 10 s per level

| Concurrency | Requests | Throughput | p50 | p95 | p99 | Errors |
|---|---|---|---|---|---|---|
| 1 | 1816 | 181.5 req/s | 5.8 ms | 8.2 ms | 9.3 ms | 0.00% |
| 8 | 1980 | 197.5 req/s | 39.5 ms | 70.3 ms | 79.7 ms | 0.00% |
| 32 | 1748 | 172.3 req/s | 151.0 ms | 241.0 ms | 284.8 ms | 0.00% |

Target: p95 under 750 ms for interactive reads: met. The spectrum endpoint computes two FFTs and two envelope spectra per request.

## Compute

| Work | Time |
|---|---|
| Training: simulate 150 machines to failure (about 30,000 windows), features, three quantile models, classifier, calibration | about 20 s |
| Features for one window (FFT, Hilbert envelope, 14 indicators) | about 0.25 ms |
| Simulator job, 20 plant-days: 960 windows generated, stored, featured and scored for 12 machines | about 3 s |
| Maintenance schedule (CP-SAT, 3 machines x 14 slots) | under 0.1 s |

Not measured: the blueprint's load case of thousands of sensors at mixed sample rates. Each machine is re-scored over its whole
history since overhaul on every ingest, which is fine for a window every six hours and would need an incremental path at higher rates.
