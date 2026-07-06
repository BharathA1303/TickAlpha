// Global Sandbox State
const state = {
    accessToken: null,
    clientInfo: null,
    activeSession: null,
    websocket: null,
    ticksData: {}, // symbol -> array of {t: time_str, p: price, v: volume}
    chartSymbol: null,
    apiBase: window.location.origin,
    totalTicksReceived: 0,
    lastTickAt: null, // Date.now() of the most recent tick_update message
    stallCheckInterval: null
};

const STALL_THRESHOLD_MS = 5000;

// Canvas Chart Configuration
const chartConfig = {
    canvas: null,
    ctx: null,
    maxTicks: 80,
    padding: { top: 40, right: 80, bottom: 40, left: 60 }
};

// Initialize Application
document.addEventListener("DOMContentLoaded", () => {
    initChart();
    setDefaultDate();
    initEventListeners();
    checkApiServerStatus();
    prepopulateParams();
});

// Confirms the API is reachable and reports whether the tick clock loop is
// running on this process (it's a separate `scheduler` process/container -
// see docker-compose.yml - so the API can be perfectly healthy while no
// clock loop anywhere is actually advancing sessions).
async function checkApiServerStatus() {
    const apiStatusEl = document.getElementById("diag-api-status");
    try {
        const res = await fetch(`${state.apiBase}/health`);
        if (!res.ok) throw new Error("Unhealthy");
        await res.json();
        apiStatusEl.innerText = "Reachable";
        apiStatusEl.style.color = "var(--accent-success)";
    } catch (e) {
        apiStatusEl.innerText = "Unreachable";
        apiStatusEl.style.color = "var(--accent-danger)";
    }
    // The clock loop's actual status can only be observed indirectly, by
    // whether an active session receives ticks - see startStallWatcher().
    document.getElementById("diag-clock-status").innerText = "Awaiting active session...";
}

function initChart() {
    chartConfig.canvas = document.getElementById("sandbox-chart-canvas");
    chartConfig.ctx = chartConfig.canvas.getContext("2d");
    
    // Resize handler
    const resizeCanvas = () => {
        const parent = chartConfig.canvas.parentElement;
        chartConfig.canvas.width = parent.clientWidth;
        chartConfig.canvas.height = parent.clientHeight;
        if (state.chartSymbol) {
            drawRealTimeChart(state.chartSymbol);
        } else {
            drawChartPlaceholder("Select an instrument from list to view trend");
        }
    };
    
    window.addEventListener("resize", resizeCanvas);
    setTimeout(resizeCanvas, 200); // initial sizing
}

function prepopulateParams() {
    const params = new URLSearchParams(window.location.search);
    const clientId = params.get("client_id");
    const clientSecret = params.get("client_secret");

    if (clientId) {
        document.getElementById("client-id").value = clientId;
    }
    if (clientSecret) {
        document.getElementById("client-secret").value = clientSecret;
    }

    if (clientId && clientSecret) {
        logToTerminal("Sandbox: Credentials detected in URL. Auto-connecting...");
        connectKey();
    }
}

function setDefaultDate() {
    const dateInput = document.getElementById("session-date");
    const fourDaysAgo = new Date();
    fourDaysAgo.setDate(fourDaysAgo.getDate() - 4);
    dateInput.value = fourDaysAgo.toISOString().split("T")[0];
}

function initEventListeners() {
    // Key connection form
    document.getElementById("key-connect-form").addEventListener("submit", async (e) => {
        e.preventDefault();
        await connectKey();
    });

    // Manual symbol add button
    document.getElementById("btn-add-manual").addEventListener("click", () => {
        addManualInstrument();
    });

    // Session form submit (Start simulation)
    document.getElementById("sandbox-session-form").addEventListener("submit", async (e) => {
        e.preventDefault();
        await startSimulation();
    });

    // Session stop button
    document.getElementById("btn-stop-simulation").addEventListener("click", () => {
        stopSimulation();
    });
}

// Connect API credentials
async function connectKey() {
    const clientId = document.getElementById("client-id").value.trim();
    const clientSecret = document.getElementById("client-secret").value;
    const badge = document.getElementById("conn-status-badge");
    const statusText = document.getElementById("conn-status-text");
    const btnConnect = document.getElementById("btn-connect");

    badge.className = "status-badge verifying";
    statusText.innerText = "Verifying...";
    btnConnect.disabled = true;
    logToTerminal("System: Verifying Client ID and Secret...");

    try {
        const res = await fetch(`${state.apiBase}/v1/auth/token`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ client_id: clientId, client_secret: clientSecret })
        });

        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || "Authentication failed");
        }

        const data = await res.json();
        state.accessToken = data.access_token;

        // Fetch details of token key by querying /v1/sessions schema or by analyzing payload
        // Since we can query key scopes or we know auth is successful:
        // We'll store verified state info
        state.clientInfo = {
            clientId: clientId,
            scopes: parseJwtScopes(state.accessToken)
        };

        badge.className = "status-badge connected";
        statusText.innerText = "Authorized";
        btnConnect.innerText = "Connected & Active";
        logToTerminal("Success: Key verified. Access granted.", "success");

        // Show session creator and scopes list
        document.getElementById("session-setup-card").classList.remove("hidden");
        document.getElementById("key-details-container").classList.remove("hidden");
        
        renderScopesUI(state.clientInfo.scopes);
        
    } catch (err) {
        badge.className = "status-badge failed";
        statusText.innerText = "Auth Failed";
        btnConnect.disabled = false;
        logToTerminal(`Error: Authentication failed - ${err.message}`, "error");
        alert(err.message);
    }
}

// Simple client-side JWT scopes parser
function parseJwtScopes(token) {
    try {
        const base64Url = token.split('.')[1];
        const base64 = base64Url.replace(/-/g, '+').replace(/_/g, '/');
        const jsonPayload = decodeURIComponent(atob(base64).split('').map(function(c) {
            return '%' + ('00' + c.charCodeAt(0).toString(16)).slice(-2);
        }).join(''));
        const payload = JSON.parse(jsonPayload);
        
        // Show speed limit and allowed symbols from token claims if present
        if (payload.max_replay_speed) {
            document.getElementById("lbl-max-speed").innerText = `${payload.max_replay_speed}x`;
            const speedSelect = document.getElementById("session-speed");
            // Set speed select options cap
            Array.from(speedSelect.options).forEach(opt => {
                if (parseInt(opt.value) > payload.max_replay_speed) {
                    opt.disabled = true;
                }
            });
        } else {
            document.getElementById("lbl-max-speed").innerText = "60x (Max)";
        }
        
        if (payload.allowed_symbols && payload.allowed_symbols.length > 0) {
            document.getElementById("lbl-allowed-symbols").innerText = payload.allowed_symbols.join(", ");
        } else {
            document.getElementById("lbl-allowed-symbols").innerText = "All symbols allowed";
        }
        
        return payload.scopes || ["nse:eq"];
    } catch (e) {
        return ["nse:eq"];
    }
}

function renderScopesUI(scopes) {
    const list = document.getElementById("key-scopes-list");
    list.innerHTML = scopes.map(s => `<span class="scope-badge">${s}</span>`).join("");
}

// Add manual instrument specification
function addManualInstrument() {
    const input = document.getElementById("manual-symbol");
    const spec = input.value.trim().toUpperCase();
    if (!spec) return;

    // Verify format EXCHANGE:SEGMENT:SYMBOL
    const parts = spec.split(":");
    if (parts.length !== 3) {
        alert("Invalid symbol format! Must be EXCHANGE:SEGMENT:SYMBOL (e.g. NSE:FUT:NIFTY)");
        return;
    }

    const container = document.getElementById("presets-container");
    
    // Check if already exists
    const existing = Array.from(container.querySelectorAll("input")).find(el => el.value === spec);
    if (existing) {
        alert("Symbol already listed!");
        return;
    }

    // Add element to preset list
    const div = document.createElement("div");
    div.className = "instrument-item";
    
    const isDeriv = parts[1] === "FUT" || parts[1] === "OPT";
    const badgeClass = isDeriv ? "fo" : "";
    const badgeText = isDeriv ? parts[1] : `${parts[0]} ${parts[1]}`;

    div.innerHTML = `
        <input type="checkbox" id="sym-manual-${spec.replace(/:/g, '_')}" value="${spec}" checked>
        <label for="sym-manual-${spec.replace(/:/g, '_')}">
            <span>${parts[2]}</span>
            <span class="instrument-type-badge ${badgeClass}">${badgeText}</span>
        </label>
    `;
    container.appendChild(div);
    input.value = "";
    logToTerminal(`System: Added custom instrument ${spec} to subscriber list.`);
}

// Start simulation session
async function startSimulation() {
    const targetDate = document.getElementById("session-date").value;
    const replaySpeed = parseInt(document.getElementById("session-speed").value);
    
    // Check subscribed checkboxes
    const checkedBoxes = Array.from(document.querySelectorAll("#presets-container input[type='checkbox']:checked"));
    const symbols = checkedBoxes.map(cb => cb.value);

    if (symbols.length === 0) {
        alert("Please subscribe to at least one instrument!");
        return;
    }

    setStartLoadingState(true);
    logToTerminal(`System: Launching virtual replay session on date ${targetDate}...`);

    try {
        // Step 1: Create session
        const createRes = await fetch(`${state.apiBase}/v1/sessions`, {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                "Authorization": `Bearer ${state.accessToken}`
            },
            body: JSON.stringify({ date: targetDate, replay_speed: replaySpeed })
        });

        if (!createRes.ok) {
            const err = await createRes.json();
            throw new Error(err.detail || "Session creation failed");
        }

        const session = await createRes.json();
        state.activeSession = session;
        logToTerminal(`System: Session created successfully with ID: ${session.session_id}`);

        // Step 2: Subscribe symbols
        logToTerminal(`System: Subscribing to ${symbols.length} instruments...`);
        const subRes = await fetch(`${state.apiBase}/v1/sessions/${session.session_id}/subscribe`, {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                "Authorization": `Bearer ${state.accessToken}`
            },
            body: JSON.stringify({ symbols: symbols })
        });

        if (!subRes.ok) {
            const err = await subRes.json();
            throw new Error(err.detail || "Symbol subscription failed");
        }
        
        state.activeSession = await subRes.json();
        logToTerminal("System: Symbols cached & preloaded in simulation engine.", "success");

        // Step 3: Start Clock
        logToTerminal("System: Resuming virtual replay clocks...");
        const startRes = await fetch(`${state.apiBase}/v1/sessions/${session.session_id}/start`, {
            method: "POST",
            headers: { "Authorization": `Bearer ${state.accessToken}` }
        });

        if (!startRes.ok) throw new Error("Clock start request failed");
        state.activeSession = await startRes.json();

        // Step 4: Open WebSocket Connection
        await initWebSocketFeed(session.session_id);

        // Render UI panels
        document.getElementById("sandbox-clock-container").classList.remove("hidden");
        document.getElementById("btn-start-simulation").classList.add("hidden");
        document.getElementById("btn-stop-simulation").classList.remove("hidden");

        // Select first active symbol for charting
        state.chartSymbol = state.activeSession.subscriptions[0];
        document.getElementById("lbl-active-symbol").innerText = state.chartSymbol.split(":")[2];
        document.getElementById("lbl-active-spec").innerText = state.chartSymbol;

        initLtpFeedUI(state.activeSession.subscriptions);

        // Reset & show real session diagnostics (session id, date, tick counter, staleness)
        state.totalTicksReceived = 0;
        state.lastTickAt = null;
        document.getElementById("session-diag-strip").classList.remove("hidden");
        document.getElementById("diag-session-id").innerText = state.activeSession.session_id;
        document.getElementById("diag-session-date").innerText = state.activeSession.date;
        document.getElementById("diag-tick-count").innerText = "0";
        document.getElementById("diag-last-tick-age").innerText = "Waiting for first tick...";
        document.getElementById("diag-clock-status").innerText = "Waiting for first tick...";
        startStallWatcher();

    } catch (err) {
        logToTerminal(`Error: Failed to initiate simulation - ${err.message}`, "error");
        alert(err.message);
        setStartLoadingState(false);
    }
}

function setStartLoadingState(isLoading) {
    const btn = document.getElementById("btn-start-simulation");
    if (isLoading) {
        btn.disabled = true;
        btn.innerText = "Synthesizing Ticks Path...";
    } else {
        btn.disabled = false;
        btn.innerText = "⚡ Create & Start Simulation";
    }
}

// WebSocket Setup
async function initWebSocketFeed(sessionId) {
    try {
        logToTerminal("System: Requesting WebSocket feed token...");
        const res = await fetch(`${state.apiBase}/v1/auth/feed-token`, {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                "Authorization": `Bearer ${state.accessToken}`
            },
            body: JSON.stringify({ session_id: sessionId })
        });

        if (!res.ok) throw new Error("Failed to acquire WebSocket feed token");
        
        const data = await res.json();
        const feedToken = data.feed_token;

        const wsUrl = `${state.apiBase.replace("http", "ws")}/v1/feed?token=${feedToken}`;
        logToTerminal("System: Handshaking WebSocket connection...");
        
        state.websocket = new WebSocket(wsUrl);
        
        state.websocket.onopen = () => {
            logToTerminal("WebSocket: Connection established.", "success");
            document.getElementById("log-connection-type").innerText = "WebSocket Streaming";
        };
        
        state.websocket.onmessage = (event) => {
            const message = JSON.parse(event.data);
            if (message.type === "tick_update") {
                processTickUpdate(message);
            }
        };
        
        state.websocket.onerror = (e) => {
            logToTerminal("WebSocket Error occurred. Check that the API server is reachable and the feed token hasn't expired (60s single-use).", "error");
        };

        state.websocket.onclose = () => {
            logToTerminal("WebSocket: Connection closed.");
            document.getElementById("log-connection-type").innerText = "Offline";
            stopStallWatcher();
        };

    } catch (e) {
        logToTerminal(`Error: WebSocket setup failed - ${e.message}`, "error");
        throw e;
    }
}

// Process ticks updates
function processTickUpdate(msg) {
    // Update digital Clock
    document.getElementById("sandbox-clock-val").innerText = msg.virtual_time;

    const ticksPayload = msg.ticks;
    let tickCountThisMessage = 0;

    // Merge ticks data
    for (const spec in ticksPayload) {
        if (!state.ticksData[spec]) {
            state.ticksData[spec] = [];
        }

        const receivedTicks = ticksPayload[spec];
        tickCountThisMessage += receivedTicks.length;
        state.ticksData[spec].push(...receivedTicks);

        // Cap local data storage length
        if (state.ticksData[spec].length > chartConfig.maxTicks) {
            state.ticksData[spec].shift();
        }

        // Update LTP list values with flash effect
        if (receivedTicks.length > 0) {
            updateLtpItemValue(spec, receivedTicks[receivedTicks.length - 1]);
        }
    }

    // Record real tick-arrival state for the diagnostics strip / stall watcher
    state.lastTickAt = Date.now();
    state.totalTicksReceived += tickCountThisMessage;
    document.getElementById("diag-tick-count").innerText = String(state.totalTicksReceived);
    document.getElementById("diag-clock-status").innerText = "Running";
    document.getElementById("diag-clock-status").style.color = "var(--accent-success)";
    document.getElementById("stall-warning").classList.add("hidden");

    // Refresh active chart symbol
    if (state.chartSymbol && ticksPayload[state.chartSymbol]) {
        drawRealTimeChart(state.chartSymbol);
    }

    // Check if session completed
    if (msg.status === "completed") {
        logToTerminal("System: Simulation date replayed completely.", "success");
        stopSimulation();
    }
}

// Polls once a second while a session is active: if a WebSocket is open but
// no tick_update has arrived in STALL_THRESHOLD_MS, the server-side tick
// clock loop is very likely not running (it's a separate `scheduler`
// process/container in docker-compose.yml, distinct from the `api` process
// this page talks to - see ENABLE_SIMULATOR_LOOP). Surfaces that distinction
// directly instead of leaving the chart silently frozen with no explanation.
function startStallWatcher() {
    stopStallWatcher();
    state.stallCheckInterval = setInterval(() => {
        if (!state.lastTickAt) return; // still waiting on the very first tick
        const ageMs = Date.now() - state.lastTickAt;
        document.getElementById("diag-last-tick-age").innerText = `${(ageMs / 1000).toFixed(1)}s ago`;

        const stallWarning = document.getElementById("stall-warning");
        if (ageMs > STALL_THRESHOLD_MS) {
            stallWarning.classList.remove("hidden");
            document.getElementById("diag-clock-status").innerText = "Stalled";
            document.getElementById("diag-clock-status").style.color = "var(--accent-danger)";
        }
    }, 1000);
}

function stopStallWatcher() {
    if (state.stallCheckInterval) {
        clearInterval(state.stallCheckInterval);
        state.stallCheckInterval = null;
    }
}

// Initialize Tickers UI
function initLtpFeedUI(subscriptions) {
    const list = document.getElementById("ltp-feed-list");
    
    list.innerHTML = subscriptions.map(spec => {
        const parts = spec.split(":");
        const isDeriv = parts[1] === "FUT" || parts[1] === "OPT";
        const badgeClass = isDeriv ? "fo" : "";
        const activeClass = state.chartSymbol === spec ? "active" : "";
        
        return `
            <div class="price-item ${activeClass}" id="ltp-item-${spec.replace(/:/g, '_')}" onclick="changeActiveChartSymbol('${spec}')">
                <div class="price-sym-block">
                    <span class="price-sym">${parts[2]}</span>
                    <span class="price-seg-badge badge ${badgeClass}">${parts[0]}:${parts[1]}</span>
                </div>
                <div class="price-info-block">
                    <span class="price-val" id="ltp-val-${spec.replace(/:/g, '_')}">--</span>
                    <span class="price-vol" id="ltp-vol-${spec.replace(/:/g, '_')}">Vol: 0</span>
                </div>
            </div>
        `;
    }).join("");
}

function changeActiveChartSymbol(spec) {
    state.chartSymbol = spec;
    
    // Set active item class
    document.querySelectorAll(".price-item").forEach(item => {
        item.classList.remove("active");
    });
    const selected = document.getElementById(`ltp-item-${spec.replace(/:/g, '_')}`);
    if (selected) selected.classList.add("active");

    document.getElementById("lbl-active-symbol").innerText = spec.split(":")[2];
    document.getElementById("lbl-active-spec").innerText = spec;
    
    if (state.ticksData[spec] && state.ticksData[spec].length > 0) {
        drawRealTimeChart(spec);
    } else {
        drawChartPlaceholder("Waiting for stream ticks data...");
    }
}

function updateLtpItemValue(spec, lastTick) {
    const idSafeSpec = spec.replace(/:/g, '_');
    const valEl = document.getElementById(`ltp-val-${idSafeSpec}`);
    const volEl = document.getElementById(`ltp-vol-${idSafeSpec}`);
    
    if (!valEl) return;

    const oldPrice = parseFloat(valEl.innerText) || null;
    const newPrice = lastTick.p;

    valEl.innerText = newPrice.toFixed(2);
    if (volEl && lastTick.v) {
        volEl.innerText = `Vol: ${lastTick.v}`;
    }

    // Dynamic Flash Animations
    if (oldPrice !== null) {
        if (newPrice > oldPrice) {
            valEl.className = "price-val flash-up";
        } else if (newPrice < oldPrice) {
            valEl.className = "price-val flash-down";
        }
        
        // Remove animation class after short timeout
        setTimeout(() => {
            valEl.className = "price-val";
        }, 300);
    }
}

// Stop simulation session
async function stopSimulation() {
    logToTerminal("System: Stopping simulation and closing stream...");

    stopStallWatcher();

    if (state.websocket) {
        state.websocket.close();
        state.websocket = null;
    }

    if (state.activeSession) {
        // Call pause backend API just in case
        try {
            await fetch(`${state.apiBase}/v1/sessions/${state.activeSession.session_id}/pause`, {
                method: "POST",
                headers: { "Authorization": `Bearer ${state.accessToken}` }
            });
        } catch (e) {
            // silent ignore
        }
    }

    state.activeSession = null;
    state.ticksData = {};
    state.chartSymbol = null;
    state.totalTicksReceived = 0;
    state.lastTickAt = null;

    // Update elements
    document.getElementById("sandbox-clock-container").classList.add("hidden");
    document.getElementById("session-diag-strip").classList.add("hidden");
    document.getElementById("stall-warning").classList.add("hidden");
    document.getElementById("btn-start-simulation").classList.remove("hidden");
    document.getElementById("btn-stop-simulation").classList.add("hidden");
    setStartLoadingState(false);

    document.getElementById("ltp-feed-list").innerHTML = `
        <div class="price-item empty" style="justify-content: center; color: var(--text-secondary); font-size: 13px;">No active stream</div>
    `;

    drawChartPlaceholder("Select an instrument & subscribe to start feed");
}

// Canvas chart drawer helper (Candlestick candlestick-style trend line)
function drawChartPlaceholder(text) {
    const canvas = chartConfig.canvas;
    const ctx = chartConfig.ctx;
    
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.fillStyle = "#0c101d";
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    
    ctx.fillStyle = "#90a4ae";
    ctx.font = "14px 'Outfit', sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(text, canvas.width / 2, canvas.height / 2);
}

function drawRealTimeChart(symbol) {
    const ticks = state.ticksData[symbol] || [];
    if (ticks.length < 2) {
        drawChartPlaceholder("Waiting for more ticks data...");
        return;
    }
    
    const canvas = chartConfig.canvas;
    const ctx = chartConfig.ctx;
    const pad = chartConfig.padding;
    const width = canvas.width;
    const height = canvas.height;
    
    ctx.clearRect(0, 0, width, height);
    
    // Draw Background
    ctx.fillStyle = "#090d16";
    ctx.fillRect(0, 0, width, height);
    
    // Calculate price boundaries
    const prices = ticks.map(t => t.p);
    const minP = Math.min(...prices) * 0.9995;
    const maxP = Math.max(...prices) * 1.0005;
    const priceRange = maxP - minP || 1;
    
    // Chart coordinate conversion functions
    const getX = (index) => {
        const plotWidth = width - pad.left - pad.right;
        return pad.left + (index / (chartConfig.maxTicks - 1)) * plotWidth;
    };
    
    const getY = (price) => {
        const plotHeight = height - pad.top - pad.bottom;
        return height - pad.bottom - ((price - minP) / priceRange) * plotHeight;
    };
    
    // Draw grid lines & Y-axis labels
    ctx.strokeStyle = "#172237";
    ctx.lineWidth = 1;
    ctx.fillStyle = "#90a4ae";
    ctx.font = "10px monospace";
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    
    const gridLines = 4;
    for (let i = 0; i <= gridLines; i++) {
        const priceVal = minP + (i / gridLines) * priceRange;
        const y = getY(priceVal);
        
        ctx.beginPath();
        ctx.moveTo(pad.left, y);
        ctx.lineTo(width - pad.right, y);
        ctx.stroke();
        
        // Write price label on the right
        ctx.fillText(priceVal.toFixed(2), pad.left - 10, y);
    }
    
    // Draw X-axis timeline markers (Time ticks)
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    const labelSpacing = Math.max(1, Math.floor(ticks.length / 5));
    for (let i = 0; i < ticks.length; i += labelSpacing) {
        const tick = ticks[i];
        const x = getX(i);
        ctx.fillText(tick.t, x, height - pad.bottom + 8);
    }

    // Draw Price Path Area (Gradient fill)
    ctx.beginPath();
    ctx.moveTo(getX(0), getY(prices[0]));
    for (let i = 1; i < ticks.length; i++) {
        ctx.lineTo(getX(i), getY(prices[i]));
    }
    
    // Connect to bottom for area fill
    ctx.lineTo(getX(ticks.length - 1), height - pad.bottom);
    ctx.lineTo(getX(0), height - pad.bottom);
    ctx.closePath();
    
    const fillGrd = ctx.createLinearGradient(0, pad.top, 0, height - pad.bottom);
    fillGrd.addColorStop(0, "rgba(99, 102, 241, 0.25)");
    fillGrd.addColorStop(1, "rgba(99, 102, 241, 0.00)");
    ctx.fillStyle = fillGrd;
    ctx.fill();
    
    // Draw Price Line (Indigo/Purple stroke)
    ctx.beginPath();
    ctx.moveTo(getX(0), getY(prices[0]));
    for (let i = 1; i < ticks.length; i++) {
        ctx.lineTo(getX(i), getY(prices[i]));
    }
    ctx.strokeStyle = "#818cf8";
    ctx.lineWidth = 2.5;
    ctx.stroke();
    
    // Draw Volume Bars at the bottom
    const maxV = Math.max(...ticks.map(t => t.v)) || 1;
    const volHeight = 40;
    ctx.globalAlpha = 0.25;
    
    for (let i = 0; i < ticks.length; i++) {
        const tick = ticks[i];
        const barHeight = (tick.v / maxV) * volHeight;
        const x = getX(i) - 2;
        const y = height - pad.bottom - barHeight;
        
        if (i === 0 || prices[i] >= prices[i - 1]) {
            ctx.fillStyle = "#10b981"; // green
        } else {
            ctx.fillStyle = "#ef4444"; // red
        }
        
        ctx.fillRect(x, y, 4, barHeight);
    }
    ctx.globalAlpha = 1.0;
    
    // Draw Last Price Marker dot
    const lastIdx = ticks.length - 1;
    const lastX = getX(lastIdx);
    const lastY = getY(prices[lastIdx]);
    
    ctx.beginPath();
    ctx.arc(lastX, lastY, 4, 0, 2 * Math.PI);
    ctx.fillStyle = "#818cf8";
    ctx.fill();
    
    ctx.beginPath();
    ctx.arc(lastX, lastY, 8, 0, 2 * Math.PI);
    ctx.strokeStyle = "rgba(129, 140, 248, 0.5)";
    ctx.lineWidth = 1.5;
    ctx.stroke();
}

function logToTerminal(message, type = "info") {
    const terminal = document.getElementById("sandbox-terminal-log");
    const entry = document.createElement("div");
    entry.className = `log-entry ${type}`;
    
    const timeStr = new Date().toLocaleTimeString();
    entry.innerHTML = `<span class="timestamp">[${timeStr}]</span> ${message}`;
    
    terminal.appendChild(entry);
    terminal.scrollTop = terminal.scrollHeight;
}
