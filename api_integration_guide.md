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
*   **Example Request**:
    `GET /v1/price/NSE/RELIANCE?segment=EQ`

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
