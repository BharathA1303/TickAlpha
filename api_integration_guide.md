# AlphaSync Data Layer - API Integration Guide

This guide details how external applications (such as client frontends, dashboards, or algorithmic trading bots) can consume data from the AlphaSync Dedicated Delayed-Data Layer.

**Live base URL**: `http://147.93.168.157:8003` (WebSocket at `ws://147.93.168.157:8003`)
Local development default: `http://localhost:8000`

---

## 1. Compliance Delay Gate Rule
To comply with regulatory guidelines:
*   **3-Day Rolling Delay**: Only data with a trading date older than **3 rolling days** from the current date is accessible.
*   Any query specifying a range that crosses into the restricted 3-day window will be automatically clipped to the compliance cutoff boundary.
*   Any request specifically targeting a date within the restricted 3-day window will return a `400 Bad Request`.

---

## 1a. Data Freshness Guarantees

If you are integrating this data layer into another platform, here is what you can rely on:

*   **Historical data is permanent and never silently overwritten.** Once a trading day's EOD record is ingested, it is retained indefinitely. If an exchange later issues a corrected bhavcopy for a day you've already queried (this does happen, though rarely), the old value is **not** replaced in place — it's kept as a superseded version, and the corrected value becomes the new current version. `GET /v1/price/{exchange}/{symbol}/range` and the "latest price" endpoint always return the current (corrected, if applicable) version automatically. See Section 4D to inspect the correction history of a specific day if you need it.
*   **New data arrives automatically every day.** A backend job ingests each day's NSE and BSE EOD data at **19:00 IST**, so the dataset self-extends daily without any action needed from you. If a run is missed (e.g. brief downtime), the same job automatically backfills any gap found in the trailing 7 trading days on its next run — you do not need to request backfills yourself.
*   **Tick-by-tick data is simulated, not live.** Because this service operates under the 3-day compliance delay, "tick-by-tick real-time" means a deterministic Brownian Bridge simulation seeded from that day's real EOD OHLCV (Section 5 covers the exact mechanics), replayed at your chosen speed — not a live market feed. The same `(symbol, date, EOD version)` always reproduces the same tick path, so a replay session you've already started is never disrupted even if that day's EOD data is corrected later — you keep seeing exactly what you started with. A *new* session created after a correction will naturally replay the corrected values instead.
*   **Check ingestion health proactively.** `GET /v1/ingestion-status/health` (requires a key with `admin` scope) reports whether the last 7 trading days ingested successfully, and flags degraded status if a source was blocked/unreachable. If you operate a downstream platform on top of this data, polling this endpoint (e.g. once every morning) is the recommended way to detect staleness before your own users do — see Section 4F.

---

## 1b. Asset Coverage: Equities, F&O, and Commodities

The API's authentication/scoping, delay gate, historical range queries, correction/versioning behavior, and tick-by-tick replay simulation all work **identically** across every segment below — a client integrating against `NSE:OPT:RELIANCE` (an options contract) writes the same code as one integrating against `NSE:EQ:RELIANCE` (the underlying stock), just with `segment`, `expiry`, `strike`, and `option_type` filled in. What differs by segment is only how real the underlying data is:

| Exchange | Segment | Coverage | Notes |
|---|---|---|---|
| `NSE` | `EQ` (cash equities) | **Real**, auto-updated nightly | Real NSE bhavcopy |
| `BSE` | `EQ` (cash equities) | **Real**, auto-updated nightly | Real BSE bhavcopy |
| `NSE` | `FUT` (futures, stocks & indices) | **Real**, auto-updated nightly | Real expiries and open interest (no strike — futures don't have one) |
| `NSE` | `OPT` (options, stocks & indices) | **Real**, auto-updated nightly | Real strikes, real expiries, real open interest, full option chains per underlying — not one synthetic contract |
| `MCX` | `FUT` (commodities) | **Simulated only** — no real feed yet | Available for development/testing via the sandbox seeder, not sourced from a real MCX feed. If your integration needs real commodity prices, don't rely on `MCX` data yet. |

For F&O contracts, use the `expiry`, `strike`, and `option_type` query parameters documented in Section 4B/4C to target a specific contract (e.g. `GET /v1/price/NSE/RELIANCE?segment=OPT&expiry=2026-06-25&strike=2450&option_type=CE`). `GET /v1/symbols/NSE?segment=FUT` or `?segment=OPT` lists what's currently available for a given segment.

---

## 2. Getting API Credentials (Admin-Provisioned)

Client API keys are **no longer self-service** — only the platform administrator can issue them. Before you can integrate, an admin must log in to the Admin Console (the site's web UI, gated behind an admin login) and create a key for you, specifying:

*   **Scopes** — which exchange/segments you may access (`nse:eq`, `bse:eq`, `nse:fut`, `nse:opt`, `mcx:fut`, `cds:fut`, etc.)
*   **Allowed symbols** — an optional allowlist restricting the key to specific instruments (e.g. `NSE:EQ:RELIANCE`). Left empty, the key can access any symbol within its granted scopes.
*   **Max replay speed** — the fastest tick playback multiplier (1x–60x) sessions created with this key are permitted to request.
*   **Rate limit** — requests/minute.

The admin can also **pause**, **disable**, or **delete** your key at any time. If your key stops working mid-integration with a `403 Forbidden` ("Client account is deactivated") or an unexpected `403` on a specific symbol/speed, check with the admin — this is expected behavior, not a bug.

You will receive a **Client ID** and a **Client Secret** (shown only once at creation time) to use below.

---

## 3. Authentication Flow

The Data Layer enforces authentication using JWT (JSON Web Tokens).

### Step 1: Generate a JWT Access Token
Send a `POST` request to `/v1/auth/token` with your Client ID and Client Secret.

*   **Endpoint**: `POST /v1/auth/token`
*   **Content-Type**: `application/json`
*   **Request Body**:
    ```json
    {
      "client_id": "YOUR_CLIENT_ID",
      "client_secret": "YOUR_CLIENT_SECRET"
    }
    ```
*   **Response**:
    ```json
    {
      "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
      "token_type": "bearer",
      "expires_in": 3600
    }
    ```
*   **Errors**: `401 Unauthorized` if credentials are wrong; `403 Forbidden` if the key has been paused, disabled, or deleted by an admin.

### Step 2: Include Token in Requests
Add the returned `access_token` in the `Authorization` header of all subsequent API calls:
```http
Authorization: Bearer <access_token>
```
Tokens expire after 3600 seconds (1 hour) — re-authenticate with Step 1 to get a fresh one.

---

## 4. REST API Reference

All REST endpoints require the `Authorization` bearer header. Every price/session endpoint additionally enforces:
*   **Scope check**: your key must have the `{exchange}:{segment}` scope (e.g. `nse:eq`) — otherwise `403 Forbidden`.
*   **Symbol allowlist**: if your key was provisioned with a restricted `allowed_symbols` list, requests for any other symbol return `403 Forbidden`.

### A. Fetch Symbols List
Retrieve all distinct symbols available for a specific exchange and segment.

*   **Endpoint**: `GET /v1/symbols/{exchange}`
*   **Path Parameters**:
    *   `exchange`: `NSE`, `BSE`, or `MCX`
*   **Query Parameters**:
    *   `segment` (optional): `EQ` (default), `FUT`, or `OPT`
*   **Example Request**:
    `GET /v1/symbols/NSE?segment=EQ`

### B. Fetch Latest EOD Price (Single Record)
Returns the most recent eligible (compliance-gated) daily OHLCV record for a symbol.

*   **Endpoint**: `GET /v1/price/{exchange}/{symbol}`
*   **Path Parameters**:
    *   `exchange`: `NSE`, `BSE`, or `MCX`
    *   `symbol`: e.g. `RELIANCE`
*   **Query Parameters**:
    *   `segment` (optional): `EQ` (default), `FUT`, or `OPT`
    *   `expiry`, `strike`, `option_type` (optional): for derivatives contracts
*   **Example Request (equity)**:
    `GET /v1/price/NSE/RELIANCE?segment=EQ`
*   **Example Request (a specific option contract — real strikes/expiries, see Section 1b)**:
    `GET /v1/price/NSE/RELIANCE?segment=OPT&expiry=2026-06-25&strike=2450&option_type=CE`
*   **Example Request (futures contract)**:
    `GET /v1/price/NSE/RELIANCE?segment=FUT&expiry=2026-06-25`

### C. Query Historical EOD Ranges
Fetch historical daily open/high/low/close/volume (OHLCV) data for a specific asset over a custom time range.

*   **Endpoint**: `GET /v1/price/{exchange}/{symbol}/range`
*   **Path Parameters**:
    *   `exchange`: `NSE`, `BSE`, or `MCX`
    *   `symbol`: e.g. `RELIANCE` (for equities) or `GOLD` (for commodities)
*   **Query Parameters**:
    *   `start`: Start date (`YYYY-MM-DD`)
    *   `end`: End date (`YYYY-MM-DD`)
    *   `segment` (optional): `EQ` (default), `FUT`, or `OPT`
    *   `expiry`, `strike`, `option_type` (optional): for derivatives contracts
*   **Example Request**:
    `GET /v1/price/NSE/RELIANCE/range?start=2026-05-01&end=2026-06-30&segment=EQ`
*   **Example JSON Response**:
    ```json
    [
      {
        "id": 15,
        "symbol": "RELIANCE",
        "exchange": "NSE",
        "segment": "EQ",
        "expiry": null,
        "strike": null,
        "option_type": null,
        "open_interest": null,
        "market_timestamp": "2026-06-01",
        "open": 2420.5,
        "high": 2450.0,
        "low": 2415.2,
        "close": 2442.3,
        "volume": 2854000,
        "ingested_at": "2026-06-02T13:00:00+00:00"
      }
    ]
    ```
*   Every record now also includes `"version"` (integer, starts at 1) and `"is_current"` (always `true` for records returned by this endpoint, since range queries only return current versions — see Section 1a and D below).

### D. View Correction History For a Single Day
Returns every stored version of one day's EOD record (oldest first), including any superseded (corrected) versions — use this if `GET /v1/price/{exchange}/{symbol}/range` shows a value that seems to have changed since you last checked it, and you want to see exactly what changed.

*   **Endpoint**: `GET /v1/price/{exchange}/{symbol}/history`
*   **Path Parameters**:
    *   `exchange`: `NSE`, `BSE`, or `MCX`
    *   `symbol`: e.g. `RELIANCE`
*   **Query Parameters**:
    *   `date` (required): the trading date to inspect (`YYYY-MM-DD`), subject to the same 3-day delay gate as other endpoints.
    *   `segment`, `expiry`, `strike`, `option_type` (optional): same meaning as other price endpoints.
*   **Example Request**:
    `GET /v1/price/NSE/RELIANCE/history?date=2026-06-01`
*   **Example JSON Response** (a day that was corrected once):
    ```json
    {
      "exchange": "NSE",
      "segment": "EQ",
      "symbol": "RELIANCE",
      "market_timestamp": "2026-06-01",
      "version_count": 2,
      "was_corrected": true,
      "versions": [
        { "version": 1, "close": 2420.5, "is_current": false, "superseded_at": "2026-06-04T19:00:12+00:00", "...": "..." },
        { "version": 2, "close": 2431.0, "is_current": true,  "superseded_at": null, "...": "..." }
      ]
    }
    ```
*   If the day was never corrected, `version_count` is `1` and `was_corrected` is `false`.

### E. View Raw Ingestion Log (Admin Keys Only)
Returns the raw audit trail of ingestion runs (one entry per exchange per day), including any `error_message` recorded on failure.

*   **Endpoint**: `GET /v1/ingestion-status`
*   **Requires**: a key with the `admin` scope.
*   **Query Parameters**:
    *   `limit` (optional, default `20`, max `100`): number of log records to return, most recent first.
*   **Example Request**:
    `GET /v1/ingestion-status?limit=10`

### F. Check Ingestion Health (Admin Keys Only)
Reports whether the nightly data-update job is current, so you can detect an upstream ingestion problem (e.g. NSE/BSE source blocked) instead of unknowingly serving stale "latest price" data to your users.

*   **Endpoint**: `GET /v1/ingestion-status/health`
*   **Requires**: a key with the `admin` scope.
*   **Query Parameters**:
    *   `lookback_days` (optional, default `7`, max `30`): how many trailing trading days to check.
*   **Example Request**:
    `GET /v1/ingestion-status/health?lookback_days=7`
*   **Example JSON Response**:
    ```json
    {
      "status": "healthy",
      "lookback_days": 7,
      "last_successful_ingestion_date": "2026-07-03",
      "days_since_last_success": 1,
      "problem_trading_days": [],
      "message": "Ingestion is up to date."
    }
    ```
*   If `status` is `"degraded"`, `problem_trading_days` lists the specific dates that had missing or zero-row ingestion — check `GET /v1/ingestion-status` (Section 4E) for the `error_message` detail on those dates.

---

## 5. Replay Sessions & WebSocket Live Stream

To stream tick-by-tick real-time simulated market data, you must establish a **Virtual Replay Session**.

### Step 1: Create a Session
Create a virtual replay session targeting a specific historical date (subject to the 3-day delay cutoff) and speed.

*   **Endpoint**: `POST /v1/sessions`
*   **Body**:
    ```json
    {
      "date": "2026-06-15",
      "replay_speed": 5
    }
    ```
*   **Response**: Returns a `session_id` (e.g. `sess_8ac8d72f928e100f`).
*   **Note**: `replay_speed` (1x–60x) cannot exceed the **max replay speed** your admin configured for your key. Requesting a higher speed returns `400 Bad Request`.

### Step 2: Subscribe to Instruments
Subscribes the session to specific symbols. The backend will automatically pre-cache simulated high-resolution ticks for the day. Symbols outside your key's allowed scopes/symbol allowlist are rejected (or silently skipped when using a wildcard subscription).

*   **Endpoint**: `POST /v1/sessions/{session_id}/subscribe`
*   **Body**:
    ```json
    {
      "symbols": [
        "NSE:EQ:RELIANCE",
        "NSE:FUT:TCS",
        "MCX:FUT:GOLD"
      ]
    }
    ```
*   *Note: Supports wildcard specifications like `NSE:EQ:ALL` or `ALL` to subscribe to all scoped assets.*

### Step 3: Get a WebSocket Feed Token
Request a short-lived, single-use token to establish a secure WebSocket channel.

*   **Endpoint**: `POST /v1/auth/feed-token`
*   **Body**:
    ```json
    {
      "session_id": "sess_8ac8d72f928e100f"
    }
    ```
*   **Response**: Returns a short-lived `feed_token`.

### Step 4: Connect WebSocket
Open a WebSocket connection to the feed endpoint:
`ws://147.93.168.157:8003/v1/feed?token=YOUR_FEED_TOKEN` (or `ws://localhost:8000/...` for local dev)

### Step 5: Start Session Clock
Trigger playback to begin streaming real-time ticks over the WebSocket:
*   **Endpoint**: `POST /v1/sessions/{session_id}/start`

### Sessions Run Continuously — Not Restricted to Market Hours
A session's virtual clock simulates one trading day (09:15–15:30) at a time, but this is **not** a real-world market-hours restriction: you can create, start, and stream a session at any hour, any day. Once a session's virtual clock reaches simulated market close, it does **not** stop — it automatically rolls over to the next available trading day (still respecting the 3-day compliance delay) and resets its virtual clock to `09:15:00`, continuing to stream indefinitely. If no later day has data, it wraps back around to the earliest available day rather than going idle. This means a session can simply be left running to continuously exercise your integration without being recreated every simulated day.

When a rollover happens, you'll receive a distinct WebSocket message instead of the usual `tick_update`:
```json
{
  "type": "day_rollover",
  "session_id": "sess_8ac8d72f928e100f",
  "previous_date": "2026-06-15",
  "date": "2026-06-16",
  "virtual_time": "09:15:00",
  "status": "active",
  "ticks": {}
}
```
Handle this alongside `tick_update` in your WebSocket message handler if your UI wants to visibly reflect the day change (e.g. resetting a chart's x-axis); otherwise it's safe to ignore and ticks will simply keep flowing for the new date.

---

## 6. JavaScript / Node.js Integration Example

```javascript
const WebSocket = require('ws'); // Use browser's native WebSocket if in front-end

const API_BASE = "http://147.93.168.157:8003"; // or "http://localhost:8000" for local dev
const WS_BASE = "ws://147.93.168.157:8003";     // or "ws://localhost:8000" for local dev
const CREDENTIALS = {
  client_id: "your_client_id_here",
  client_secret: "your_client_secret_here"
};

async function runIntegration() {
  try {
    // 1. Authenticate & Obtain JWT
    console.log("Authenticating...");
    const authRes = await fetch(`${API_BASE}/v1/auth/token`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(CREDENTIALS)
    });
    const { access_token } = await authRes.json();
    console.log("JWT Access Token generated.");

    // 2. Create a Replay Session (e.g. 5 days ago, at 10x playback speed)
    const sessionDate = new Date();
    sessionDate.setDate(sessionDate.getDate() - 5);
    const dateStr = sessionDate.toISOString().split('T')[0];

    const sessionRes = await fetch(`${API_BASE}/v1/sessions`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Authorization": `Bearer ${access_token}`
      },
      body: JSON.stringify({ date: dateStr, replay_speed: 10 })
    });
    const { session_id } = await sessionRes.json();
    console.log(`Launched Replay Session: ${session_id}`);

    // 3. Subscribe to Symbols
    await fetch(`${API_BASE}/v1/sessions/${session_id}/subscribe`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Authorization": `Bearer ${access_token}`
      },
      body: JSON.stringify({ symbols: ["NSE:EQ:RELIANCE", "NSE:EQ:INFY"] })
    });
    console.log("Subscribed to RELIANCE and INFY.");

    // 4. Request WebSocket Feed Token
    const feedTokenRes = await fetch(`${API_BASE}/v1/auth/feed-token`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Authorization": `Bearer ${access_token}`
      },
      body: JSON.stringify({ session_id })
    });
    const { feed_token } = await feedTokenRes.json();

    // 5. Connect WebSocket
    const ws = new WebSocket(`${WS_BASE}/v1/feed?token=${feed_token}`);

    ws.on('open', async () => {
      console.log("WebSocket connected. Starting clock playback...");
      // Start the virtual replay session streaming
      await fetch(`${API_BASE}/v1/sessions/${session_id}/start`, {
        method: "POST",
        headers: { "Authorization": `Bearer ${access_token}` }
      });
    });

    ws.on('message', (rawData) => {
      const data = JSON.parse(rawData);
      if (data.type === "tick_update") {
        console.log(`Virtual Time: ${data.virtual_time} | Status: ${data.status}`);
        for (const symbol in data.ticks) {
          const ticks = data.ticks[symbol];
          ticks.forEach(tick => {
            console.log(`  -> TICK ${symbol} | Price: ${tick.p} | Volume: ${tick.v}`);
          });
        }
      } else if (data.type === "day_rollover") {
        // The session never stops at market close - it automatically moves on
        // to the next trading day and keeps streaming. See Section 5.
        console.log(`Day rolled over: ${data.previous_date} -> ${data.date}. Clock reset to ${data.virtual_time}.`);
      }
    });

    ws.on('close', () => console.log("WebSocket connection closed."));
  } catch (error) {
    console.error("Integration failed:", error);
  }
}

runIntegration();
```

---

## 7. Python Integration Example

```python
import requests
import json
import time
import asyncio
import websockets
from datetime import datetime, timedelta

API_BASE = "http://147.93.168.157:8003"  # or "http://localhost:8000" for local dev
WS_BASE = "ws://147.93.168.157:8003"      # or "ws://localhost:8000" for local dev
CLIENT_ID = "your_client_id_here"
CLIENT_SECRET = "your_client_secret_here"

def get_headers(token):
    return {"Authorization": f"Bearer {token}"}

async def main():
    # 1. Authenticate & Obtain JWT
    auth_resp = requests.post(
        f"{API_BASE}/v1/auth/token",
        json={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}
    )
    access_token = auth_resp.json()["access_token"]
    headers = get_headers(access_token)
    print("JWT Token Generated.")

    # 2. Create Replay Session (e.g. 4 days ago at 5x speed)
    replay_date = (datetime.now() - timedelta(days=4)).strftime("%Y-%m-%d")
    sess_resp = requests.post(
        f"{API_BASE}/v1/sessions",
        headers=headers,
        json={"date": replay_date, "replay_speed": 5}
    )
    session_id = sess_resp.json()["session_id"]
    print(f"Session {session_id} created for date {replay_date}.")

    # 3. Subscribe to Instruments
    requests.post(
        f"{API_BASE}/v1/sessions/{session_id}/subscribe",
        headers=headers,
        json={"symbols": ["NSE:EQ:RELIANCE", "MCX:FUT:GOLD"]}
    )
    print("Subscribed to instruments.")

    # 4. Request Feed Token
    token_resp = requests.post(
        f"{API_BASE}/v1/auth/feed-token",
        headers=headers,
        json={"session_id": session_id}
    )
    feed_token = token_resp.json()["feed_token"]

    # 5. Connect WebSocket and start playback
    async with websockets.connect(f"{WS_BASE}/v1/feed?token={feed_token}") as websocket:
        print("WebSocket channel opened.")
        
        # Trigger Play/Resume session playback
        requests.post(f"{API_BASE}/v1/sessions/{session_id}/start", headers=headers)
        print("Session playback started.")
        
        # Listen for ticks
        try:
            while True:
                msg_raw = await websocket.recv()
                msg = json.loads(msg_raw)
                if msg.get("type") == "tick_update":
                    print(f"[{msg['virtual_time']}] Status: {msg['status']}")
                    for symbol, ticks in msg.get("ticks", {}).items():
                        for tick in ticks:
                            print(f"  {symbol}: Price {tick['p']} | Vol {tick['v']}")
                elif msg.get("type") == "day_rollover":
                    # Session never stops at market close - it rolls over to
                    # the next trading day and keeps streaming. See Section 5.
                    print(f"Day rolled over: {msg['previous_date']} -> {msg['date']}. Clock reset to {msg['virtual_time']}.")
        except websockets.exceptions.ConnectionClosed:
            print("WebSocket connection closed.")

if __name__ == "__main__":
    asyncio.run(main())
```

---

## 8. Error Codes Reference

| Status | Meaning | Common Cause |
|---|---|---|
| `400 Bad Request` | Request violates the 3-day compliance gate, or `replay_speed` exceeds your key's max allowed speed | Requested date/speed is not permitted |
| `401 Unauthorized` | Missing, invalid, or expired JWT / bad client credentials | Re-authenticate via `/v1/auth/token` |
| `403 Forbidden` | Key lacks the required scope for this exchange/segment, key's symbol allowlist excludes the requested symbol, or key has been paused/disabled/deleted by an admin | Contact your admin to check key status and permissions |
| `404 Not Found` | Symbol/session/data not found for the given parameters | Verify symbol spelling and that EOD data exists for that date |
| `429 Too Many Requests` | Exceeded your key's `rate_limit_per_min` | Back off and retry, or ask your admin to raise the limit |

---

## 9. Rate Limits & Best Practices
*   Cache JWTs client-side and reuse them until the 1-hour expiry rather than calling `/v1/auth/token` on every request.
*   Feed tokens (`/v1/auth/feed-token`) are single-use and expire in 60 seconds — request one immediately before opening the WebSocket.
*   Prefer targeted symbol subscriptions over `ALL` wildcards where possible to reduce tick cache generation load.
*   The backend runs behind multiple stateless HTTP/WebSocket workers backed by shared Redis/Postgres state — you don't need to pin your requests to a specific server or worry about which one you land on. WebSocket reconnects are safe: your feed token flow (Section 5, Steps 3-4) always establishes a fresh, valid connection regardless of which backend worker handles it.
*   If you're building a platform on top of this API rather than a single app, poll `GET /v1/ingestion-status/health` (Section 4F) once daily (e.g. right after your own morning cache warm-up) so a blocked upstream data source surfaces to you automatically instead of your users noticing stale prices first.
