# DECISIONS.md — Architecture Decision Records

> Every major design choice in FactorySense, with rationale for the 48-hour
> constraint and how it would be architected for 1,000+ devices in production.

---

## 1. The "Why" Behind the Data Model

### The "Stateful" Approach: Why a `device_state` Table?

The first design question was: **where do the consecutive-breach counters live?**

The alternative I rejected was a **windowed query** — on every incoming POST, run something like:

```sql
SELECT COUNT(*) FROM telemetry_readings
WHERE device_id = :id
  AND temperature_c > 75
  AND timestamp >= (
    SELECT timestamp FROM telemetry_readings
    WHERE device_id = :id
    ORDER BY timestamp DESC
    LIMIT 1 OFFSET 2   -- "last 3 readings"
  )
ORDER BY timestamp DESC;
```

This fails for two reasons specific to this project. First, "consecutive" isn't the same as "most recent 3 that breach" — a single normal reading in the middle resets the streak, and detecting that with SQL requires a gap-detection query (ROW_NUMBER + LAG), which is complex and slow. Second, this query runs on **every single POST** — at 1 reading per device per 10 seconds with 3 devices, that's manageable, but at 1,000 devices it becomes 100 subqueries/sec against an ever-growing table.

Instead, I chose a dedicated `device_state` table — one row per device, updated in-place:

> **By tracking consecutive breaches as counters in a dedicated state table, we achieve O(1) complexity for alert evaluation. We avoid expensive windowed queries (scanning the last 50 rows) on every single ingestion, which preserves database performance as telemetry frequency increases.**

On every POST, the logic is: read one row → increment or reset two integer columns → write one row. No subqueries, no aggregations. The full alert decision costs exactly **1 SQL read + 1 SQL write** per ingestion, regardless of how many historical readings exist.

**Tradeoff acknowledged**: Inline counters don't support retroactive replay. If we needed to ask "what was the alert state at 3am yesterday?", we'd have to re-ingest all readings from that timestamp. For a live monitoring system where the *current* state is the product, O(1) wins.

### Schema

| Table | Purpose | Key Columns |
|---|---|---|
| `telemetry_readings` | Append-only time-series log | `device_id`, `timestamp`, `temperature_c`, `vibration_g`, `received_at` |
| `device_state` | One row per device — FSM state + breach counters | `device_id` (PK), `alert_state`, `alert_type`, `last_seen`, `consecutive_temp_breaches`, `consecutive_vibe_breaches` |

### Indexing

`telemetry_readings` has a **composite index on `(device_id, timestamp)`**. The `GET /status/{device_id}` query is:

```sql
SELECT * FROM telemetry_readings
WHERE device_id = :id
ORDER BY timestamp DESC
LIMIT 50;
```

Without the composite index, this is a full-table scan that sorts every row in the table. With the index, the database walks the B-tree directly to the device's rows and reads the last 50 in order — an index-range scan. As the table grows to millions of rows across hundreds of devices, response latency stays flat because the index size for one device is constant.

---

## 2. The Alert State Machine (Deduplication)

### The State Machine

```
                    breach detected
    ┌─────────┐ ─────────────────────► ┌─────────┐
    │ NORMAL  │                        │  ALERT  │  → send ONE WhatsApp
    │ state=0 │ ◄───────────────────── │ state=1 │  → send ONE "Resolved"
    └─────────┘     breach cleared     └─────────┘
                                         │     ▲
                                         │     │
                                         └─────┘
                                     breach continues
                                       → SUPPRESS
                                      (send nothing)
```

### How Deduplication Works

The `device_state.alert_state` column is the single source of truth. The four possible transitions:

1. **Normal (0) + breach threshold met → Alert (1)**: Flip state, fire Twilio. **The only path that generates a message.**
2. **Alert (1) + breach continues → Alert (1)**: State is unchanged, so we do nothing. This is deduplication — the POST handler exits without calling Twilio.
3. **Alert (1) + readings return to normal → Normal (0)**: Flip state, fire "Resolved" Twilio. One message, no more.
4. **Normal (0) + normal reading → Normal (0)**: No-op.

> **Deduplication is handled via a state-check gate. Before sending a Twilio request, the engine verifies the current `alert_state`. We only trigger a notification on a transition (Normal → Alert or Alert → Normal). This ensures the factory owner receives exactly one message per incident.**

The critical insight is: **the transition is the trigger, not the state**. An infinitely breaching device in ALERT state sends zero additional messages — the `check_alerts()` function reads `alert_state = 1`, sees no transition, and returns immediately without touching Twilio.

Because `check_alerts()` is called synchronously inside the POST handler and within the same SQLAlchemy session, the `device_state` row is locked for the duration of the transaction. No concurrent POST for the same device can read a stale `alert_state` in between.

### Breach Thresholds — Why These Numbers?

The thresholds (temp ≥ 3 consecutive, vibration ≥ 5 consecutive) came directly from the challenge spec. But the design choice I made was to implement them as **separate counters that are independent and reset independently**.

An alternative was a single `consecutive_breach_count` with an `alert_type` field. I rejected this because: if a device is hot (temp_breaches = 2) and then vibrates (vibe_breaches = 1), a single counter would combine unrelated events and either fire too early or require complex type-tracking logic. Separate counters keep each alert type's streak clean and allow simultaneous alerts (if both thresholds are hit in the same reading, temperature fires first because motor heat is the higher-priority condition).

---

## 3. Silent-Failure Detection (The "Hard" Part)

### The Challenge

Silent failure is harder to implement than temperature/vibration alerts because it inverts the entire detection model.

- **Temp/Vibe alerts**: data *arrives* → server reacts.
- **Silence alerts**: data *stops* → server must act proactively.

A POST endpoint is reactive by definition. A silent device sends no requests, so no request handler ever runs. The server cannot detect the absence of future POSTs from inside a POST handler — it has to detect it from somewhere else entirely.

This creates a second problem: the detection logic must now live in **two separate execution contexts** — the request handler and a background worker — and both must agree on the FSM state. A device already in `ALERT` (silence) must not receive a second silence alert on the next poll cycle. Getting this wrong means the factory owner gets a WhatsApp message every 30 seconds until the device recovers.

### The Solution

> **I implemented an asynchronous background worker (`silent_failure_checker`) using `asyncio`. It runs on a 30-second heartbeat, independent of the FastAPI request-response cycle. It queries for devices where `last_seen < (now - 120s)`.**

```python
async def silent_failure_checker():
    """Background worker: detects devices that have stopped sending telemetry."""
    while True:
        await asyncio.sleep(30)
        stale_cutoff = datetime.utcnow() - timedelta(seconds=120)
        # Run sync SQLAlchemy query in thread pool — never block the event loop
        stale_devices = await asyncio.get_event_loop().run_in_executor(
            None, get_stale_devices, stale_cutoff
        )
        for device in stale_devices:
            await asyncio.get_event_loop().run_in_executor(
                None, check_alerts, device.device_id, silence=True
            )
```

**Why `run_in_executor` and not `await`?** SQLAlchemy's synchronous session is not async-compatible. Calling it directly from a coroutine blocks the entire asyncio event loop — every incoming POST request would stall while the worker waits for disk I/O. `run_in_executor` offloads the blocking call to a thread pool, so the event loop stays free.

**Why 30-second poll interval for a 120-second threshold?** The worst-case detection latency is poll_interval (30s). A device that dies at T=0 is detected between T=30 and T=150 (120s threshold + up to 30s wait). 30 seconds is a reasonable operational latency for a factory floor — a machine that's been offline for 2.5 minutes needs attention. Polling more frequently wastes I/O; polling less frequently risks missing a brief outage entirely.

### Edge Case: Device Comes Back Online

> **When a silent device finally sends a packet, it immediately triggers the "Resolved" logic.**

Exact flow:
1. Worker fires at T=150: `alert_state` flips to 1 (ALERT/silence). Twilio fires.
2. Device recovers, sends POST at T=163.
3. `check_alerts()` runs: reads `alert_state = 1`, checks current reading — no breach, silence flag is false.
4. Transition: Alert → Normal. `alert_state` flips to 0. "Device recovered" Twilio fires.

Two messages total. Exactly correct.

**The dangerous race** — the worker reads `last_seen`, decides to fire, then a POST arrives and updates `last_seen` *before* the worker commits the `device_state` change — would result in a silence alert firing for a device that's actively sending. This is mitigated by wrapping the worker's staleness check and its `device_state` write in the same SQLAlchemy session transaction. The row-level lock prevents the concurrent POST from committing in between the worker's read and write.

---

## 4. Scaling to 1,000+ Devices (Production Readiness)

### What Breaks First (and at What Load)

| Component | Breaks At | Root Cause |
|---|---|---|
| **SQLite** | ~100 concurrent writes | Single global write lock — all writers queue serially |
| **In-process asyncio worker** | First server crash | Worker dies with the process; no restart, no handoff |
| **Synchronous Twilio in POST handler** | ~10 simultaneous alerts | 500ms × 10 = 5s of blocked ingestion |
| **No device-state cache** | ~500 devices | 500 reads/sec from SQLite on disk, competing with writes |

### Database → TimescaleDB (PostgreSQL)

> **I would migrate from SQLite to TimescaleDB (PostgreSQL extension). Its "Hypertables" are designed for high-velocity time-series data like ESP32 telemetry.**

At 1,000 devices × 1 reading/10s = **100 writes/sec** into `telemetry_readings`. SQLite serializes every write through a single lock — at 100 writes/sec this introduces queuing latency and drops packets during bursts. PostgreSQL uses row-level locking, handles 100 writes/sec trivially, and scales to thousands with connection pooling via PgBouncer.

TimescaleDB adds one critical feature: **hypertables** — automatic time-based partitioning. The query `SELECT * FROM telemetry_readings WHERE device_id = X ORDER BY timestamp DESC LIMIT 50` can be constrained to the current time partition, making it orders of magnitude faster on a billion-row table than a plain PostgreSQL query.

### Concurrency → Celery + Redis Task Queue

> **At 1,000 devices, I would move alert processing to a task queue like Celery with Redis. This prevents the API from slowing down if the Twilio API experiences latency.**

The scenario I'm solving: **1,000 devices all go silent at once** (e.g., a network partition). The current `asyncio` worker would attempt 1,000 sequential Twilio HTTP calls inside one poll cycle. Each Twilio call takes ~300ms. At 1,000 calls, that's **5 minutes** to clear the queue — during which the worker is blocked and new silence events queue up.

With Celery: the worker publishes 1,000 task messages to Redis in milliseconds, then returns. A fleet of Celery workers (scaled independently) consumes the queue in parallel, rate-limited to respect Twilio's API limits. The ingestion endpoint is never touched.

### Caching → Redis for Breach Counters

> **I would use Redis to store the "consecutive breach counters." Reading from memory is faster than hitting the disk for every incoming packet.**

Every POST currently does: read `device_state` from SQLite → evaluate → write back. At 100 writes/sec, that's **100 SQL reads/sec** on a disk-backed database, competing with the write workload. Redis stores `device:<id>:state` as a hash with sub-millisecond read latency. The hot path becomes: read from Redis (memory) → write back to Redis → async write-through to PostgreSQL for durability. On a Redis restart or cache miss, we fall back to PostgreSQL and re-warm the cache.

### Load Balancing → NGINX + Stateless FastAPI

The FastAPI app is **stateless by design** — every POST reads from the database, evaluates, writes back, and returns. No in-process state is consulted. This means 3+ instances behind NGINX can serve any request without session affinity. NGINX also handles SSL termination and per-device rate limiting to protect against malformed sensors flooding the endpoint.

### Proposed Production Architecture

```
ESP32s ──► NGINX (SSL, rate-limit) ──► FastAPI × 3 (stateless)
                                              │
                              ┌───────────────┼──────────────────┐
                              ▼               ▼                  ▼
                      TimescaleDB      Redis Cache         Redis Streams
                      (durable store)  (device state)     (event bus)
                                                                  │
                                          ┌───────────────────────┤
                                          ▼                       ▼
                                    Celery Worker           Celery Beat
                                    (alert engine)        (silence poller)
                                          │
                                          ▼
                                    SQS Queue
                                          │
                                          ▼
                                  Notification Worker
                              (Twilio, rate-limited, retries)
```

**The FSM logic in `check_alerts()` does not change at any scale.** It was written pure and dependency-free for exactly this reason — it can be lifted into a Celery task with no modification.

---

## 5. Constraints & Tradeoffs (The "Honesty" Section)

### SQLite

> **Used for its zero-configuration nature to meet the 48-hour deployment deadline. In production, persistent cloud storage (RDS/Managed Postgres) is required.**

The constraint was real: a zero-dependency deployment that a reviewer can run with `uvicorn main:app` after a single `pip install`. SQLite delivered that. No Docker, no Postgres server, no `.env` database URL required. For 3 devices at 0.1 writes/sec, the write lock is never contested and the file never exceeds a few MB.

The sacrifice was deliberate: SQLite has no network access, no replication, and no connection pooling. On Railway (the deployment target), the SQLite file lives on an ephemeral filesystem — a dyno restart wipes all historical readings. For a demo this is acceptable; for production it is not.

### Synchronous Twilio Calls

> **Twilio calls are currently synchronous for simplicity. At scale, these would be moved to background workers to keep `/telemetry` response times under 50ms.**

A Twilio WhatsApp POST takes approximately 300–500ms to complete (network + API processing). In the current implementation, this blocks the FastAPI POST handler — the ESP32 simulator's request hangs for half a second every time an alert fires. For a demo with 3–4 alert events total, this is imperceptible.

At 1,000 devices, a mass-breach event (e.g., a power surge trips the temperature threshold on all sensors simultaneously) would queue 1,000 synchronous Twilio calls through the ingestion endpoint — effectively a self-inflicted denial of service. The solution is to decouple notification from ingestion: the POST handler writes to a queue and returns `200 OK` immediately; a separate process handles Twilio at a controlled rate.

### Full Tradeoff Summary

| Decision | What We Did | What We Sacrificed | Why It's OK at Demo Scale |
|---|---|---|---|
| **SQLite** | Zero-config file database | Concurrent writes, persistence across restarts | 3 devices × 0.1 writes/sec = trivial |
| **Inline breach counters** | O(1) alert evaluation | Retroactive replay / audit trail | Demo needs live state, not history |
| **In-process asyncio worker** | `asyncio.create_task()` | Fault isolation, independent scaling | Server IS the entire system for a demo |
| **Synchronous Twilio** | Direct HTTP call in request path | POST latency (~500ms on alert) | 3–4 alert events per demo run |
| **No authentication** | No API keys or JWT | Security posture | Not internet-facing; local only |
| **No rate limiting** | No per-device throttling | Protection from rogue sensors | 3 controlled simulators, no adversary |
| **No retry logic** | Twilio failure → log + continue | Guaranteed message delivery | Console output is the fallback |
| **Flat file structure** | All `.py` files in root | Namespace isolation for large teams | 6 files; readability wins over structure |
| **`threading.Thread` simulator** | One thread per simulated device | Memory at 10K devices | 3 threads ≈ 24MB; negligible |
| **No automated tests** | Manual verification via simulator | CI/CD regression safety | The simulator *is* the integration test |

### What I'd Add With 48 More Hours

These aren't generic wishes — each one addresses a specific gap this project has:

1. **`pytest` suite for `check_alerts()`** — the FSM has 4 transitions and 2 alert types. That's 8 test cases to cover exhaustively. The most important: verify that Alert→Alert produces zero Twilio calls.
2. **`docker-compose.yml`** with FastAPI + the simulator as separate services — so the reviewer doesn't need Python installed locally.
3. **Retry logic for Twilio** — currently a Twilio 429 (rate limit) silently drops the alert. The factory owner never knows. A simple exponential backoff with 3 retries would fix this.
4. **`received_at` vs `timestamp` drift monitoring** — the schema stores both, but nothing currently alerts if they diverge by more than 5 seconds, which would indicate clock drift on the ESP32.
5. **`/health` endpoint** — returns `{"status": "ok", "worker_alive": true, "devices_monitored": 3}`. Currently there's no way to verify the silence-detection worker is running without tailing logs.
