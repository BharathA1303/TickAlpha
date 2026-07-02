# AlphaSync Data Layer - API Integration Guide

This guide details how external applications (such as client frontends, dashboards, or algorithmic trading bots) can consume data from the AlphaSync Dedicated Delayed-Data Layer.

The data layer runs by default at `http://localhost:8000` (WebSocket at `ws://localhost:8000`).

---

## 1. Compliance Delay Gate Rule
To comply with regulatory guidelines:
*   **3-Day Rolling Delay**: Only data with a trading date older than **3 rolling days** from the current date is accessible.
*   Any query specifying a range that crosses into the restricted 3-day window will be automatically clipped to the compliance cutoff boundary.
*   Any request specifically targeting a date within the restricted 3-day window will return a `400 Bad Request`.

---

## 2. Authentication Flow

The Data Layer enforces authentication using JWT (JSON Web Tokens).

### Step 1: Generate a JWT Access Token
Send a `POST` request to `/v1/auth/token` with your Client ID and Client Secret.

*   **Endpoint**: `POST http://localhost:8000/v1/auth/token`
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

### Step 2: Include Token in Requests
Add the returned `access_token` in the `Authorization` header of all subsequent API calls:
```http
Authorization: Bearer <access_token>
```

---

## 3. REST API Reference

All REST endpoints require the `Authorization` bearer header.

### A. Fetch Symbols List
Retrieve all distinct symbols available for a specific exchange and segment.

*   **Endpoint**: `GET /v1/symbols/{exchange}`
*   **Path Parameters**:
    *   `exchange`: `NSE`, `BSE`, or `MCX`
*   **Query Parameters**:
    *   `segment` (optional): `EQ` (default), `FUT`, or `OPT`
*   **Example Request**:
    `GET http://localhost:8000/v1/symbols/NSE?segment=EQ`

### B. Query Historical EOD Ranges
Fetch historical daily close/OHLCV data for a specific asset over a custom time range. 

*   **Endpoint**: `GET /v1/price/{exchange}/{symbol}/range`
*   **Path Parameters**:
    *   `exchange`: `NSE`, `BSE`, or `MCX`
    *   `symbol`: e.g. `RELIANCE` (for equities) or `GOLD` (for commodities)
*   **Query Parameters**:
    *   `start`: Start date (`YYYY-MM-DD`)
    *   `end`: End date (`YYYY-MM-DD`)
    *   `segment` (optional): `EQ` (default), `FUT`, or `OPT`
*   **Example Request**:
    `GET http://localhost:8000/v1/price/NSE/RELIANCE/range?start=2026-05-01&end=2026-06-30&segment=EQ`
*   **Example JSON Response**:
    ```json
    [
      {
        "id": 15,
        "symbol": "RELIANCE",
        "exchange": "NSE",
        "segment": "EQ",
        "market_timestamp": "2026-06-01",
        "open": 2420.5,
        "high": 2450.0,
        "low": 2415.2,
        "close": 2442.3,
        "volume": 2854000
      }
    ]
    ```

---

## 4. Replay Sessions & WebSocket Live Stream

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

### Step 2: Subscribe to Instruments
Subscribes the session to specific symbols. The backend will automatically pre-cache simulated high-resolution ticks for the day.

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
`ws://localhost:8000/v1/feed?token=YOUR_FEED_TOKEN`

### Step 5: Start Session Clock
Trigger playback to begin streaming real-time ticks over the WebSocket:
*   **Endpoint**: `POST /v1/sessions/{session_id}/start`

---

## 5. JavaScript / Node.js Integration Example

```javascript
const WebSocket = require('ws'); // Use browser's native WebSocket if in front-end

const API_BASE = "http://localhost:8000";
const WS_BASE = "ws://localhost:8000";
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

## 6. Python Integration Example

```python
import requests
import json
import time
import asyncio
import websockets
from datetime import datetime, timedelta

API_BASE = "http://localhost:8000"
WS_BASE = "ws://localhost:8000"
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
