# AlphaSync Delayed-Data Layer (`alphasync-data-layer`)

A high-performance standalone backend data service designed to store, gate, and stream stock market data (NSE, BSE, MCX) under a strict regulatory **3-day rolling delay compliance gate**. 

This service acts as a compliance gateway proxy, serving historical range queries and real-time simulated tick feeds to other client applications via REST APIs and WebSockets.

---

## 🚀 Key Features

*   **Centralized Compliance Gate**: Enforces the 3-day rolling delay constraint at the database layer. Future data requests are blocked (400 Bad Request), and overlapping ranges are clipped automatically.
*   **High-Resolution Tick Simulator**: Simulates high-frequency market tick updates using a deterministic **Brownian Bridge mathematical model** based on EOD open, high, low, close, and volume.
*   **Real-time WebSocket Streaming**: Features a low-latency tick broadcast loop supporting replay playback speeds from 1x to 60x.
*   **UDiFF Integration**: Fully compatible with the NSE **UDiFF (Unified Distilled File Formats)** data format introduced in July 2024.
*   **Robust Security Architecture**: Protected by JWT tokens with scope-based permissions (e.g. `nse:eq`, `nse:opt`, `mcx:fut`, `admin`).
*   **Built-in Developer Portal**: Includes a local frontend sandbox UI to generate developer keys, control virtual sessions, and visualize streaming charts.

---

## 🛠️ Tech Stack
*   **Backend framework**: Python, FastAPI (Async)
*   **Primary Datastore**: PostgreSQL (Permanent historical store)
*   **Caching & Broadcast**: Redis (with local in-memory fallbacks)
*   **Math & Simulation**: NumPy, Brownian Bridge Path Engine

---

## 📂 Project Structure

```
alphasync-data-layer/
  app/
    main.py                # FastAPI application entrypoint
    config.py              # Settings loader (loads from .env)
    db/
      models.py            # SQLAlchemy database models
      session.py           # DB session and engines (sync/async)
    core/
      delay_gate.py        # Central 3-day compliance gate
      auth.py              # JWT authentication & rate limiting
      cache.py             # Caching wrappers (Redis + Memory fallback)
    simulator/
      brownian_bridge.py   # Brownian Bridge tick generator (optimized)
      simulator_manager.py # Replay clocks manager & publisher
    ingestion/
      nse_bhavcopy.py      # NSE UDiFF & Legacy zip bhavcopy parser
      bse_bhavcopy.py      # BSE csv bhavcopy parser
      run_ingestion.py     # Ingestion CLI launcher
  frontend/
    index.html             # Developer portal dashboard layout
    app.js                 # Portal interactive state and chart rendering
    style.css              # Custom layout stylesheet (with badge assets)
  scripts/
    generate_api_key.py    # CLI tool to create SHA-256 hashed API keys
    seed_historical.py     # YFinance stock downloader & F&O synthesizer
  docker-compose.yml       # Production/Local orchestrator
  Dockerfile             # Python docker build blueprint
  .env                     # Local configuration parameters
  architecture.md        # Technical architecture documentation
  api_integration_guide.md # External API consumer documentation
```

---

## 💻 Local Setup & Deployment

### 1. Prerequisites
Install [Docker Desktop](https://www.docker.com/products/docker-desktop/) on your machine.

### 2. Run with Docker Compose
Spin up the PostgreSQL database, Redis caching, and the FastAPI application using:
```bash
docker-compose up --build
```
This deploys the following services:
*   **FastAPI Backend Server**: `http://localhost:8000` (docs: `http://localhost:8000/docs`)
*   **Postgres Database**: `localhost:5432`
*   **Redis Engine**: `localhost:6379`

*Note: The FastAPI container automatically initiates database migrations and tables on startup.*

---

## 🔑 Security & Key Management

Access to the API requires a signed JWT token, generated via a Client ID and Client Secret. 

### Generating a Developer API Key
To register a client application and print the raw credentials, execute the CLI tool inside your running API container:
```bash
docker-compose exec api python scripts/generate_api_key.py --owner "alphasync-website" --scopes "nse:eq,bse:eq,nse:fut,nse:opt,mcx:fut,admin" --rate-limit 120
```

> [!WARNING]
> Copy the generated Client Secret immediately from the console output. It is stored as a SHA-256 hash in the database and **cannot be retrieved again**.

---

## 📥 Ingestion Pipelines

The Data Layer includes tools to load both simulated and official exchange datasets.

### A. Seed Sample Sandbox Data (Recommended for Local Testing)
The sandbox seeder downloads daily prices from Yahoo Finance for a sample set of major stocks, synthesizes matching options/futures contracts and commodities, and inserts them into PostgreSQL:
```bash
docker-compose exec api python scripts/seed_historical.py
```

### B. Ingest Real Exchange Data
To download the official daily Bhavcopy files from NSE's archives and BSE, specify a date:
```bash
docker-compose exec api python app/ingestion/run_ingestion.py --date 2026-06-25
```
*Note: Due to security firewalls on the public exchange websites, a paid commercial feed provider (such as TrueData) should be configured for production environments.*

---

## 📉 Running Unit Tests

Unit tests are isolated inside transaction rolls to keep the database clean. Run the test suite with:
```bash
docker-compose exec api pytest -v
```
To run tests locally outside Docker, make sure you configure your local `.env` and run:
```bash
pytest -v
```

---

## 📖 API Documentation & Integration

*   To learn how to integrate this backend with your own custom websites, trading bots, or mobile apps, see the comprehensive [api_integration_guide.md](file:///d:/VIANMAX%20DEV%20TEAM%20/Dedicated%20Data%20Layer/api_integration_guide.md).
*   For details on the system design and math implementation, see [architecture.md](file:///d:/VIANMAX%20DEV%20TEAM/Dedicated%20Data%20Layer/architecture.md).
