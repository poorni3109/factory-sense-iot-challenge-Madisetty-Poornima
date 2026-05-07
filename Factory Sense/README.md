# 🏭 FactorySense — Sensor Telemetry & Alert Pipeline

Real-time IoT telemetry pipeline that ingests ESP32 sensor data, detects anomalies via consecutive-reading breach logic, manages alert states with deduplication (zero spam), detects silent device failures, and sends WhatsApp alerts via Twilio.

## Architecture

```
ESP32 Devices ──POST /telemetry──► FastAPI Server ──► SQLite DB
                                        │
                                        ├── Alert Engine (breach detection)
                                        ├── State Machine (deduplication)
                                        ├── Background Worker (silence detection)
                                        └── Twilio WhatsApp (notifications)
```

## Quick Start (Local)

```bash
pip install -r requirements.txt
uvicorn main:app --reload --host 0.0.0.0
# In another terminal:
python simulator.py
```

## Deploy to Railway

1. Push this repo to GitHub
2. Go to [railway.app](https://railway.app) → New Project → Deploy from GitHub
3. Railway auto-detects the `Procfile` and `requirements.txt`
4. Add environment variables in Railway dashboard:
   - `TWILIO_ACCOUNT_SID`
   - `TWILIO_AUTH_TOKEN`
   - `TWILIO_WHATSAPP_FROM` (default: `whatsapp:+14155238886`)
   - `ALERT_WHATSAPP_TO` (e.g., `whatsapp:+91XXXXXXXXXX`)
5. Railway assigns a live URL — use it with the simulator:
   ```bash
   python simulator.py https://your-app.up.railway.app
   ```

## API Endpoints

| Method | Path | Description |
|---|---|---|
| `POST` | `/telemetry` | Ingest `{device_id, timestamp, temperature_c, vibration_g}` |
| `GET` | `/devices/{device_id}/status` | Last 50 readings + current alert state |
| `GET` | `/health` | Health check |
| `GET` | `/docs` | Swagger UI (interactive API explorer) |

## Alert Rules

| Rule | Threshold | Consecutive Readings |
|---|---|---|
| Temperature | > 75°C | 3+ |
| Vibration | > 2.5g | 5+ |
| Silence | No data | 120+ seconds |

## State Machine (Zero Spam)

```
Normal ──breach──► Alert  (send ONE WhatsApp alert)
Alert  ──breach──► Alert  (SUPPRESS — no message)
Alert  ──clear───► Normal (send ONE "Resolved" WhatsApp)
```

## Simulator — Device 3 Fault Sequence

| Phase | Time | Behavior | Expected Alert |
|---|---|---|---|
| 1 | 0-30s | Normal | — |
| 2 | 30-70s | High temp (80-95°C) | 🔴 Temp Alert |
| 3 | 70-100s | Normal | 🔵 Temp Resolved |
| 4 | 100-170s | High vibration (3-4.5g) | 🔴 Vibe Alert |
| 5 | 170-200s | Normal | 🔵 Vibe Resolved |
| 6 | 200-335s | **Silence** | 🔴 Silence Alert |
| 7 | 335-370s | Resume normal | 🔵 Silence Resolved |

## Project Structure

```
├── main.py              # FastAPI app + endpoints
├── database.py          # SQLAlchemy engine + session
├── models.py            # ORM models + Pydantic schemas
├── alert_engine.py      # Alert state machine + Twilio + background worker
├── simulator.py         # 3-device simulator
├── requirements.txt     # Python dependencies
├── Procfile             # Railway deployment command
├── runtime.txt          # Python version for Railway
├── .env                 # Twilio credentials (local only, gitignored)
├── .gitignore
├── DECISIONS.md         # Architecture decision records
└── README.md
```

## Tech Stack

- **FastAPI** — async web framework
- **SQLAlchemy 2.0** — ORM (sync engine)
- **SQLite** — embedded database
- **Twilio** — WhatsApp Business API (sandbox)
- **Pydantic v2** — request/response validation
- **asyncio** — background silent-failure detection
