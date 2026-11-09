import asyncio
import platform
import sys
import time
import uuid
from datetime import date, timedelta

import httpx

from helpers import CA, GATEWAY, admin_api, register_client

DURATION = float(sys.argv[1]) if len(sys.argv) > 1 else 60.0
LIST_QUERY = ("query MisPedidos { misPedidos(primeros: 10) { edges { node { id estado total items { nombre cantidad precioUnitario "
              "producto { descripcion disponible } } } } pageInfo { hasNextPage endCursor } } }")


def percentile(values, q):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * q))] if ordered else float("nan")


async def run(name, workers, action, rate=None):
    latencies, errors = [], 0
    stop = time.monotonic() + DURATION
    interval = workers / rate if rate else 0.0

    async def worker(index):
        nonlocal errors
        await asyncio.sleep(interval * index / workers)
        while time.monotonic() < stop:
            started = time.perf_counter()
            try:
                ok = await action(index)
            except Exception:
                ok = False
            elapsed = time.perf_counter() - started
            latencies.append(elapsed * 1000)
            if not ok:
                errors += 1
            if interval > elapsed:
                await asyncio.sleep(interval - elapsed)

    started = time.monotonic()
    await asyncio.gather(*(worker(i) for i in range(workers)))
    elapsed = time.monotonic() - started
    return {"name": name, "workers": workers, "requests": len(latencies), "rps": len(latencies) / elapsed,
            "p50": percentile(latencies, 0.50), "p95": percentile(latencies, 0.95), "p99": percentile(latencies, 0.99),
            "error_pct": 100 * errors / max(1, len(latencies))}


def async_client(api=None):
    client = httpx.AsyncClient(base_url=GATEWAY, verify=CA, timeout=30, limits=httpx.Limits(max_connections=100))
    if api is not None:
        client.cookies = api.http.cookies
    return client


async def main():
    print(f"Entorno: {platform.platform()} · Python {platform.python_version()} · duración {DURATION:.0f} s por escenario")
    print("Preparando usuarios (registro por la API real)…")
    clients = [register_client() for _ in range(10)]
    admin = admin_api()
    results = []

    anonymous = [async_client() for _ in range(50)]
    results.append(await run("L1 catálogo, estrés (S10)", 50, lambda i: _ok(anonymous[i].get("/api/v1/products"))))
    results.append(await run("L1b catálogo, 50 req/s (S10)", 50, lambda i: _ok(anonymous[i].get("/api/v1/products")), rate=50))

    async_clients = [async_client(api) for api, _, _ in clients]
    csrfs = [api.csrf for api, _, _ in clients]

    async def purchase(i):
        response = await async_clients[i].post(
            "/api/v1/orders", headers={"X-CSRF-Token": csrfs[i], "Idempotency-Key": uuid.uuid4().hex},
            json={"items": [{"productId": "p-0001", "quantity": 1}], "paymentMethod": "deposito"})
        return response.status_code == 201
    results.append(await run("L2 crear pedido (S15)", 10, purchase))

    reports = [async_client(admin) for _ in range(5)]
    today = date.today()
    window = {"from": (today - timedelta(days=30)).isoformat(), "to": today.isoformat(), "size": "50"}
    results.append(await run("L3 reporte de ventas (S24)", 5, lambda i: _ok(reports[i].get("/api/v1/reports/sales", params=window))))

    async def graphql(i):
        k = i % len(async_clients)
        response = await async_clients[k].post("/api/v1/graphql", headers={"X-CSRF-Token": csrfs[k]},
                                               json={"operationName": "MisPedidos", "query": LIST_QUERY})
        return response.status_code == 200 and "errors" not in response.json()
    results.append(await run("L4 GraphQL misPedidos", 20, graphql))

    print()
    print(f"{'Escenario':<28}{'Usuarios':>9}{'Solicitudes':>13}{'Req/s':>8}{'p50 ms':>9}{'p95 ms':>9}{'p99 ms':>9}{'Errores':>9}")
    for r in results:
        print(f"{r['name']:<28}{r['workers']:>9}{r['requests']:>13}{r['rps']:>8.1f}{r['p50']:>9.1f}{r['p95']:>9.1f}{r['p99']:>9.1f}{r['error_pct']:>8.1f}%")
    objectives = {"L1": 200, "L1b": 200, "L2": 800, "L3": 1000, "L4": 600}
    print()
    for r in results:
        key = r["name"].split()[0]
        limit = objectives[key]
        verdict = "CUMPLE" if r["p95"] < limit and r["error_pct"] < 1 else "NO CUMPLE"
        print(f"{key}: p95 {r['p95']:.0f} ms (objetivo < {limit} ms), errores {r['error_pct']:.1f} % -> {verdict}")


async def _ok(awaitable):
    response = await awaitable
    return response.status_code == 200


asyncio.run(main())
