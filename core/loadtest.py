"""Small closed-loop load test (stdlib threads + httpx). Prints a markdown block for docs/performance.md.

    python -m core.loadtest <base_url> <token> <concurrency> <seconds> <METHOD path> [<METHOD path> ...]
"""
import os
import platform
import sys
import threading
import time

import httpx


def run(base, token, concurrency, seconds, targets):
    lat, errors, lock, stop = [], [0], threading.Lock(), time.time() + seconds

    def worker(k):
        with httpx.Client(base_url=base, headers={"Authorization": f"Bearer {token}"}, timeout=30) as c:
            i = k
            while time.time() < stop:
                method, path = targets[i % len(targets)]
                t0 = time.perf_counter()
                try:
                    ok = c.request(method, path, json={} if method == "POST" else None).status_code < 400
                except httpx.HTTPError:
                    ok = False
                with lock:
                    lat.append(time.perf_counter() - t0)
                    errors[0] += not ok
                i += 1

    threads = [threading.Thread(target=worker, args=(k,)) for k in range(concurrency)]
    t0 = time.time()
    [t.start() for t in threads]
    [t.join() for t in threads]
    lat.sort()
    q = lambda p: lat[min(len(lat) - 1, int(p * len(lat)))] * 1000      # noqa: E731
    return {"requests": len(lat), "seconds": round(time.time() - t0, 1), "rps": round(len(lat) / (time.time() - t0), 1),
            "p50_ms": round(q(0.5), 1), "p95_ms": round(q(0.95), 1), "p99_ms": round(q(0.99), 1), "error_rate": round(errors[0] / max(1, len(lat)), 4)}


if __name__ == "__main__":
    base, token, conc, secs = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
    targets = [tuple(a.split(" ", 1)) for a in sys.argv[5:]]
    print(f"Hardware: {platform.machine()}, {os.cpu_count()} cores, {platform.system()} (client and server on the same machine)\n")
    print("| Concurrency | Requests | Throughput | p50 | p95 | p99 | Errors |\n|---|---|---|---|---|---|---|")
    for n in sorted({1, conc // 4 or 1, conc}):
        r = run(base, token, n, secs, targets)
        print(f"| {n} | {r['requests']} | {r['rps']} req/s | {r['p50_ms']} ms | {r['p95_ms']} ms | {r['p99_ms']} ms | {r['error_rate']:.2%} |", flush=True)
