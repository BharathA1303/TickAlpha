// Global Application State
const state = {
    accessToken: null,
    activeSession: null,
    websocket: null,
    subscriptions: [],
    ticksData: {}, // symbol -> array of {time, price, volume}
    chartSymbol: null,
    apiBase: "http://localhost:8000"
};

// Canvas Chart Configuration
const chartConfig = {
    canvas: null,
    ctx: null,
    maxTicks: 80,
    padding: { top: 40, right: 80, bottom: 40, left: 60 }
};

// Initialize Application
document.addEventListener("DOMContentLoaded", () => {
    initNavigation();
    initForms();
    checkApiHealth();
    
    // Set default session date to 4 days ago to ensure compliance
    const dateInput = document.getElementById("session-date");
    const fourDaysAgo = new Date();
    fourDaysAgo.setDate(fourDaysAgo.getDate() - 4);
    dateInput.value = fourDaysAgo.toISOString().split("T")[0];
    
    // Initialize Canvas Chart
    chartConfig.canvas = document.getElementById("tick-chart");
    chartConfig.ctx = chartConfig.canvas.getContext("2d");
    drawChartPlaceholder("Select an instrument & subscribe to start feed");
    
    // Clipboard helper
    document.getElementById("copy-jwt-btn").addEventListener("click", () => {
        const tokenStr = document.getElementById("jwt-token-string").innerText;
        navigator.clipboard.writeText(tokenStr).then(() => {
            const btn = document.getElementById("copy-jwt-btn");
            btn.innerText = "Copied!";
            setTimeout(() => { btn.innerText = "Copy Token"; }, 2000);
        });
    });
});

// Navigation Handling
function initNavigation() {
    const navItems = document.querySelectorAll(".nav-item");
    const tabContents = document.querySelectorAll(".tab-content");
    const pageTitle = document.getElementById("page-title");

    navItems.forEach(item => {
        item.addEventListener("click", (e) => {
            e.preventDefault();
            
            // Remove active class from all nav items
            navItems.forEach(n => n.classList.remove("active"));
            // Add active to current
            item.classList.add("active");

            // Hide all sections
            tabContents.forEach(section => section.classList.remove("active"));
            // Show target section
            const targetId = item.getAttribute("href").substring(1);
            document.getElementById(`section-${targetId}`).classList.add("active");

            // Update title
            pageTitle.innerText = item.innerText.split(" ").slice(1).join(" ");
        });
    });
}

// Inquire API health check
async function checkApiHealth() {
    const healthEl = document.getElementById("api-health");
    try {
        const res = await fetch(`${state.apiBase}/health`);
        const data = await res.json();
        if (data.status === "healthy") {
            healthEl.innerText = "Online (3-day delay)";
            document.querySelector(".status-indicator").className = "status-indicator online";
        } else {
            healthEl.innerText = "Error: Non-healthy status";
            document.querySelector(".status-indicator").className = "status-indicator offline";
        }
    } catch (e) {
        healthEl.innerText = "Offline (unreachable)";
        document.querySelector(".status-indicator").className = "status-indicator offline";
    }
}

// Form Handlers
function initForms() {
    // 1. Auth Form
    document.getElementById("auth-form").addEventListener("submit", async (e) => {
        e.preventDefault();
        const clientId = document.getElementById("client-id").value.strip ? document.getElementById("client-id").value.strip() : document.getElementById("client-id").value;
        const clientSecret = document.getElementById("client-secret").value.strip ? document.getElementById("client-secret").value.strip() : document.getElementById("client-secret").value;
        
        logToTerminal(`System: Authenticating client '${clientId}'...`);
        
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
            
            // UI Updates
            document.getElementById("jwt-token-string").innerText = data.access_token;
            document.getElementById("jwt-display-card").classList.remove("hidden");
            
            const badge = document.getElementById("token-status");
            badge.querySelector(".indicator").className = "indicator green";
            badge.querySelector(".text").innerText = "Client Authenticated";
            
            logToTerminal("System: Access Token successfully generated.");
            
            // Enable controls
            document.getElementById("btn-subscribe").removeAttribute("disabled");
            
        } catch (err) {
            logToTerminal(`Error: ${err.message}`, "error-log");
            alert(err.message);
        }
    });

    // 1B. Admin Key Form
    document.getElementById("admin-key-form").addEventListener("submit", async (e) => {
        e.preventDefault();
        if (!state.accessToken) {
            alert("Please authenticate using your developer keys first to authorize administrative operations!");
            return;
        }
        
        const owner = document.getElementById("admin-owner").value.trim();
        const checkedScopes = Array.from(document.querySelectorAll('input[name="admin-scopes"]:checked')).map(cb => cb.value);
        const rateLimit = parseInt(document.getElementById("admin-rate-limit").value);
        
        logToTerminal(`Admin: Generating client API credentials for owner '${owner}'...`);
        
        try {
            const res = await fetch(`${state.apiBase}/v1/admin/keys`, {
                method: "POST",
                headers: { 
                    "Content-Type": "application/json",
                    "Authorization": `Bearer ${state.accessToken}`
                },
                body: JSON.stringify({ owner: owner, scopes: checkedScopes, rate_limit_per_min: rateLimit })
            });
            
            if (!res.ok) {
                const err = await res.json();
                throw new Error(err.detail || "Failed to generate keys");
            }
            
            const data = await res.json();
            
            // Render credentials card
            document.getElementById("generated-client-id").value = data.client_id;
            document.getElementById("generated-client-secret").value = data.client_secret;
            document.getElementById("generated-keys-card").classList.remove("hidden");
            
            logToTerminal(`Admin: Credentials successfully created for '${owner}'.`);
            
        } catch (err) {
            logToTerminal(`Admin Error: ${err.message}`, "error-log");
            alert(err.message);
        }
    });

    // Copy buttons for generated keys
    document.getElementById("btn-copy-gen-id").addEventListener("click", (e) => {
        e.preventDefault();
        const val = document.getElementById("generated-client-id").value;
        navigator.clipboard.writeText(val).then(() => {
            const btn = document.getElementById("btn-copy-gen-id");
            btn.innerText = "Copied!";
            setTimeout(() => { btn.innerText = "Copy"; }, 2000);
        });
    });

    document.getElementById("btn-copy-gen-secret").addEventListener("click", (e) => {
        e.preventDefault();
        const val = document.getElementById("generated-client-secret").value;
        navigator.clipboard.writeText(val).then(() => {
            const btn = document.getElementById("btn-copy-gen-secret");
            btn.innerText = "Copied!";
            setTimeout(() => { btn.innerText = "Copy"; }, 2000);
        });
    });

    // 2. Create Session Form
    document.getElementById("session-form").addEventListener("submit", async (e) => {
        e.preventDefault();
        if (!state.accessToken) {
            alert("Please authenticate first!");
            return;
        }
        
        const dateVal = document.getElementById("session-date").value;
        const speedVal = parseInt(document.getElementById("session-speed").value);
        
        logToTerminal(`System: Launching replay session for date ${dateVal} at ${speedVal}x speed...`);
        
        try {
            const res = await fetch(`${state.apiBase}/v1/sessions`, {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    "Authorization": `Bearer ${state.accessToken}`
                },
                body: JSON.stringify({ date: dateVal, replay_speed: speedVal })
            });
            
            if (!res.ok) {
                const err = await res.json();
                throw new Error(err.detail || "Failed to create session");
            }
            
            const data = await res.json();
            state.activeSession = data;
            
            updateSessionStateUI();
            updateSubscriptionChips();
            updateTickersSidebarList();
            logToTerminal(`System: Replay session created. Session ID: ${data.session_id}`);
            
            // Trigger WebSocket connection
            await initWebSocketFeed(data.session_id);
            
        } catch (err) {
            logToTerminal(`Error: ${err.message}`, "error-log");
            alert(err.message);
        }
    });

    // 3. Subscribe Symbol Form
    document.getElementById("subscribe-form").addEventListener("submit", async (e) => {
        e.preventDefault();
        if (!state.activeSession) {
            alert("No active session! Launch a session first.");
            return;
        }
        
        const symbolSpec = document.getElementById("sub-symbol").value.trim().toUpperCase();
        logToTerminal(`System: Subscribing to symbol '${symbolSpec}'...`);
        
        try {
            const res = await fetch(`${state.apiBase}/v1/sessions/${state.activeSession.session_id}/subscribe`, {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    "Authorization": `Bearer ${state.accessToken}`
                },
                body: JSON.stringify({ symbols: [symbolSpec] })
            });
            
            if (!res.ok) {
                const err = await res.json();
                throw new Error(err.detail || "Subscription failed");
            }
            
            const data = await res.json();
            state.activeSession = data;
            
            // Set as primary active chart symbol
            selectChartSymbol(symbolSpec);
            
            updateSessionStateUI();
            updateSubscriptionChips();
            updateTickersSidebarList();
            
            logToTerminal(`System: Subscribed successfully to ${symbolSpec}. Pre-cached ticks ready.`);
            
        } catch (err) {
            logToTerminal(`Error: ${err.message}`, "error-log");
            alert(err.message);
        }
    });

    // Bulk action button event listeners
    document.getElementById("btn-sub-all-nse").addEventListener("click", () => triggerBulkSubscription("NSE:EQ:ALL"));
    document.getElementById("btn-sub-all-mcx").addEventListener("click", () => triggerBulkSubscription("MCX:FUT:ALL"));
    document.getElementById("btn-sub-all-global").addEventListener("click", () => triggerBulkSubscription("ALL"));

    // 4. Query Range Form
    document.getElementById("history-form").addEventListener("submit", async (e) => {
        e.preventDefault();
        if (!state.accessToken) {
            alert("Please authenticate first!");
            return;
        }
        
        const symbol = document.getElementById("hist-symbol").value.trim().toUpperCase();
        const exchange = document.getElementById("hist-exchange").value;
        const segment = document.getElementById("hist-segment").value;
        const start = document.getElementById("hist-start").value;
        const end = document.getElementById("hist-end").value;
        
        document.getElementById("history-query-status").innerText = "PENDING";
        document.getElementById("history-query-status").className = "badge";
        
        try {
            const url = `${state.apiBase}/v1/price/${exchange}/${symbol}/range?start=${start}&end=${end}&segment=${segment}`;
            logToTerminal(`System: Querying EOD price range from ${url}...`);
            
            const res = await fetch(url, {
                headers: { "Authorization": `Bearer ${state.accessToken}` }
            });
            
            if (!res.ok) {
                const err = await res.json();
                throw new Error(err.detail || "Query failed");
            }
            
            const data = await res.json();
            document.getElementById("history-json-output").innerText = JSON.stringify(data, null, 2);
            document.getElementById("history-query-status").innerText = "SUCCESS";
            document.getElementById("history-query-status").className = "badge status-indicator online";
            
        } catch (err) {
            document.getElementById("history-json-output").innerText = JSON.stringify({ error: err.message }, null, 2);
            document.getElementById("history-query-status").innerText = "FAILED";
            document.getElementById("history-query-status").className = "badge status-indicator offline";
        }
    });

    // Clear logs button
    document.getElementById("btn-clear-logs").addEventListener("click", () => {
        document.getElementById("tick-log-terminal").innerHTML = "";
    });
}

// Create/Update WebSocket Connection
async function initWebSocketFeed(sessionId) {
    if (state.websocket) {
        state.websocket.close();
    }
    
    logToTerminal(`System: Requesting short-lived feed token for session ${sessionId}...`);
    
    try {
        const tokenRes = await fetch(`${state.apiBase}/v1/auth/feed-token`, {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                "Authorization": `Bearer ${state.accessToken}`
            },
            body: JSON.stringify({ session_id: sessionId })
        });
        
        if (!tokenRes.ok) {
            throw new Error("Failed to get WebSocket feed token");
        }
        
        const tokenData = await tokenRes.json();
        const feedToken = tokenData.feed_token;
        
        logToTerminal(`System: Opening WebSocket connection with token: ${feedToken.substring(0, 16)}...`);
        
        const wsUrl = `${state.apiBase.replace("http", "ws")}/v1/feed?token=${feedToken}`;
        state.websocket = new WebSocket(wsUrl);
        
        state.websocket.onopen = () => {
            logToTerminal("WebSocket: Connection established successfully.");
        };
        
        state.websocket.onmessage = (event) => {
            const message = JSON.parse(event.data);
            
            if (message.type === "tick_update") {
                // Update Clock
                document.getElementById("chart-clock").innerText = message.virtual_time;
                if (state.activeSession) {
                    state.activeSession.virtual_time = message.virtual_time;
                    state.activeSession.status = message.status;
                    updateSessionStateUI();
                }
                
                // Process ticks
                for (const symbol in message.ticks) {
                    const ticksList = message.ticks[symbol];
                    if (!state.ticksData[symbol]) {
                        state.ticksData[symbol] = [];
                    }
                    
                    ticksList.forEach(tick => {
                        state.ticksData[symbol].push(tick);
                        
                        // Keep a max buffer of ticks to prevent memory bloat
                        if (state.ticksData[symbol].length > chartConfig.maxTicks) {
                            state.ticksData[symbol].shift();
                        }
                        
                        // Output in log terminal
                        logToTerminal(`Tick: ${symbol} | Time: ${tick.t} | Price: ${tick.p.toFixed(2)} | Vol: ${tick.v}`, "incoming-tick");
                    });
                    
                    // Dynamically update the price in the tickers sidebar list directly to avoid full DOM redraws
                    const pEl = document.getElementById(`sidebar-price-${symbol.replace(/:/g, '_')}`);
                    if (pEl && ticksList.length > 0) {
                        const latestTick = ticksList[ticksList.length - 1];
                        const prevValText = pEl.innerText;
                        const prevVal = parseFloat(prevValText);
                        pEl.innerText = latestTick.p.toFixed(2);
                        
                        if (!isNaN(prevVal)) {
                            if (latestTick.p < prevVal) {
                                pEl.className = "ticker-price down";
                            } else if (latestTick.p > prevVal) {
                                pEl.className = "ticker-price";
                            }
                        }
                    }
                    
                    // Render if it's the active chart symbol
                    if (symbol === state.chartSymbol) {
                        drawRealTimeChart(symbol);
                    }
                }
                
                if (message.status === "completed") {
                    logToTerminal("WebSocket: Virtual trading session completed.", "system-log");
                }
            } else {
                logToTerminal(`WebSocket: ${JSON.stringify(message)}`, "system-log");
            }
        };
        
        state.websocket.onerror = (e) => {
            logToTerminal("WebSocket: Connection error occurred.", "error-log");
        };
        
        state.websocket.onclose = () => {
            logToTerminal("WebSocket: Connection closed.");
        };
        
    } catch (e) {
        logToTerminal(`WebSocket Error: ${e.message}`, "error-log");
    }
}

// UI Rendering Helpers
function updateSessionStateUI() {
    const container = document.getElementById("session-state-container");
    if (!state.activeSession) return;
    
    const sess = state.activeSession;
    
    // Status text mapping
    let statusClass = "yellow";
    if (sess.status === "active") statusClass = "green";
    
    container.innerHTML = `
        <div class="session-info-panel">
            <div class="info-row">
                <span class="info-label">Session ID</span>
                <span class="info-value">${sess.session_id}</span>
            </div>
            <div class="info-row">
                <span class="info-label">Replay Date</span>
                <span class="info-value">${sess.date}</span>
            </div>
            <div class="info-row">
                <span class="info-label">Virtual Clock</span>
                <span class="info-value virtual-clock">${sess.virtual_time}</span>
            </div>
            <div class="info-row">
                <span class="info-label">Status</span>
                <span class="info-value info-value-${statusClass}">${sess.status.toUpperCase()}</span>
            </div>
            <div class="info-row">
                <span class="info-label">Playback Speed</span>
                <span class="info-value">${sess.replay_speed}x</span>
            </div>
            
            <div class="session-controls">
                <button class="btn primary-btn btn-sm" id="btn-session-play" ${sess.status === 'active' || sess.status === 'completed' ? 'disabled' : ''}>Play / Resume</button>
                <button class="btn secondary-btn btn-sm" id="btn-session-pause" ${sess.status !== 'active' ? 'disabled' : ''}>Pause</button>
            </div>
        </div>
    `;
    
    // Re-bind click event handlers
    document.getElementById("btn-session-play").addEventListener("click", () => controlSessionClock("start"));
    document.getElementById("btn-session-pause").addEventListener("click", () => controlSessionClock("pause"));
    
    // Update live feed tab control buttons
    const chartPlay = document.getElementById("btn-chart-play");
    const chartPause = document.getElementById("btn-chart-pause");
    
    if (sess.status === "active") {
        chartPlay.setAttribute("disabled", "true");
        chartPause.removeAttribute("disabled");
    } else if (sess.status === "paused") {
        chartPlay.removeAttribute("disabled");
        chartPause.setAttribute("disabled", "true");
    } else {
        chartPlay.setAttribute("disabled", "true");
        chartPause.setAttribute("disabled", "true");
    }
    
    // Enable bulk subscription buttons when session is active
    document.getElementById("btn-sub-all-nse").removeAttribute("disabled");
    document.getElementById("btn-sub-all-mcx").removeAttribute("disabled");
    document.getElementById("btn-sub-all-global").removeAttribute("disabled");
}

async function controlSessionClock(action) {
    if (!state.activeSession) return;
    const sid = state.activeSession.session_id;
    
    try {
        const res = await fetch(`${state.apiBase}/v1/sessions/${sid}/${action}`, {
            method: "POST",
            headers: { "Authorization": `Bearer ${state.accessToken}` }
        });
        
        if (!res.ok) throw new Error(`Clock command '${action}' failed`);
        
        const data = await res.json();
        state.activeSession = data;
        updateSessionStateUI();
        
        logToTerminal(`System: Session clock set to ${action}.`);
    } catch (e) {
        logToTerminal(`Error: ${e.message}`, "error-log");
    }
}

function updateSubscriptionChips() {
    const chipsContainer = document.getElementById("active-subscriptions-chips");
    if (!state.activeSession || state.activeSession.subscriptions.length === 0) {
        chipsContainer.innerHTML = `<span class="chip-empty">No active subscriptions</span>`;
        return;
    }
    
    chipsContainer.innerHTML = state.activeSession.subscriptions.map(spec => {
        return `
            <span class="chip">
                <span>${spec}</span>
                <span class="chip-remove" onclick="unsubscribeSymbol('${spec}')">×</span>
            </span>
        `;
    }).join("");
}

window.unsubscribeSymbol = async function(symbolSpec) {
    if (!state.activeSession) return;
    const sid = state.activeSession.session_id;
    
    logToTerminal(`System: Unsubscribing from '${symbolSpec}'...`);
    
    try {
        const res = await fetch(`${state.apiBase}/v1/sessions/${sid}/unsubscribe`, {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                "Authorization": `Bearer ${state.accessToken}`
            },
            body: JSON.stringify({ symbols: [symbolSpec] })
        });
        
        if (!res.ok) throw new Error("Unsubscription failed");
        
        const data = await res.json();
        state.activeSession = data;
        
        if (state.chartSymbol === symbolSpec) {
            state.chartSymbol = null;
            drawChartPlaceholder("Select an instrument & subscribe to start feed");
        }
        
        updateSessionStateUI();
        updateSubscriptionChips();
        logToTerminal(`System: Unsubscribed from ${symbolSpec}.`);
    } catch (e) {
        logToTerminal(`Error: ${e.message}`, "error-log");
    }
};

function logToTerminal(message, className = "system-log") {
    const terminal = document.getElementById("tick-log-terminal");
    const entry = document.createElement("div");
    entry.className = `log-entry ${className}`;
    
    const timeStr = new Date().toLocaleTimeString();
    entry.innerHTML = `<span class="timestamp">[${timeStr}]</span> ${message}`;
    
    terminal.appendChild(entry);
    terminal.scrollTop = terminal.scrollHeight;
}

// Canvas Chart Plotter (Pure Vanilla HTML5 Canvas)
function drawChartPlaceholder(text) {
    const canvas = chartConfig.canvas;
    const ctx = chartConfig.ctx;
    
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.fillStyle = "#151e33";
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
    
    // Clear canvas
    ctx.clearRect(0, 0, width, height);
    
    // Draw Dark Card Background
    ctx.fillStyle = "#0c101d";
    ctx.fillRect(0, 0, width, height);
    
    // Calculate price boundaries
    const prices = ticks.map(t => t.p);
    const minP = Math.min(...prices) * 0.9995;
    const maxP = Math.max(...prices) * 1.0005;
    const priceRange = maxP - minP;
    
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
    ctx.strokeStyle = "#1d283d";
    ctx.lineWidth = 1;
    ctx.fillStyle = "#607d8b";
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
        ctx.fillText(tick.t.substring(0, 5), x, height - pad.bottom + 8);
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
    fillGrd.addColorStop(0, "rgba(59, 130, 246, 0.35)");
    fillGrd.addColorStop(1, "rgba(59, 130, 246, 0.00)");
    ctx.fillStyle = fillGrd;
    ctx.fill();
    
    // Draw Price Line (Blue Neon stroke)
    ctx.beginPath();
    ctx.moveTo(getX(0), getY(prices[0]));
    for (let i = 1; i < ticks.length; i++) {
        ctx.lineTo(getX(i), getY(prices[i]));
    }
    ctx.strokeStyle = "#3b82f6";
    ctx.lineWidth = 2.5;
    ctx.shadowColor = "rgba(59, 130, 246, 0.5)";
    ctx.shadowBlur = 4;
    ctx.stroke();
    
    // Reset shadow
    ctx.shadowBlur = 0;
    
    // Draw Volume Bars at the bottom
    const maxV = Math.max(...ticks.map(t => t.v)) || 1;
    const volHeight = 40; // Max height for vol bars
    ctx.globalAlpha = 0.3;
    
    for (let i = 0; i < ticks.length; i++) {
        const tick = ticks[i];
        const barHeight = (tick.v / maxV) * volHeight;
        const x = getX(i) - 2;
        const y = height - pad.bottom - barHeight;
        
        // Green volume bar if price went up/stayed same, red if price went down
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
    ctx.arc(lastX, lastY, 5, 0, 2 * Math.PI);
    ctx.fillStyle = "#3b82f6";
    ctx.fill();
    
    ctx.beginPath();
    ctx.arc(lastX, lastY, 9, 0, 2 * Math.PI);
    ctx.strokeStyle = "rgba(59, 130, 246, 0.6)";
    ctx.lineWidth = 1.5;
    ctx.stroke();
}

// Bulk subscription helper
async function triggerBulkSubscription(targetSpec) {
    if (!state.activeSession) {
        alert("No active session! Launch a session first.");
        return;
    }
    
    logToTerminal(`System: Subscribing to bulk spec '${targetSpec}'...`);
    
    try {
        const res = await fetch(`${state.apiBase}/v1/sessions/${state.activeSession.session_id}/subscribe`, {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                "Authorization": `Bearer ${state.accessToken}`
            },
            body: JSON.stringify({ symbols: [targetSpec] })
        });
        
        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || "Bulk subscription failed");
        }
        
        const data = await res.json();
        state.activeSession = data;
        
        // Auto-select the first resolved asset if no primary chart symbol is set
        if (!state.chartSymbol && data.subscriptions.length > 0) {
            selectChartSymbol(data.subscriptions[0]);
        }
        
        updateSessionStateUI();
        updateSubscriptionChips();
        updateTickersSidebarList();
        
        logToTerminal(`System: Bulk subscription complete. Active instruments: ${data.subscriptions.length}`);
    } catch (err) {
        logToTerminal(`Error: ${err.message}`, "error-log");
        alert(err.message);
    }
}

// Update tickers list in sidebar
function updateTickersSidebarList() {
    const container = document.getElementById("tickers-list-container");
    if (!state.activeSession || state.activeSession.subscriptions.length === 0) {
        container.innerHTML = `<div class="ticker-item empty">No active subscriptions</div>`;
        return;
    }
    
    container.innerHTML = state.activeSession.subscriptions.map(spec => {
        const parts = spec.split(":");
        const exchange = parts[0];
        const segment = parts[1];
        const symbol = parts[2];
        const lastTicks = state.ticksData[spec] || [];
        const lastPriceVal = lastTicks.length > 0 ? lastTicks[lastTicks.length - 1].p : null;
        const lastPriceText = lastPriceVal !== null ? lastPriceVal.toFixed(2) : "--";
        
        let priceClass = "ticker-price";
        if (lastTicks.length >= 2) {
            const pCurrent = lastTicks[lastTicks.length - 1].p;
            const pPrev = lastTicks[lastTicks.length - 2].p;
            if (pCurrent < pPrev) {
                priceClass += " down";
            }
        }
        
        const activeClass = state.chartSymbol === spec ? "active" : "";
        
        return `
            <div class="ticker-item ${activeClass}" onclick="selectChartSymbol('${spec}')">
                <div class="ticker-sym-info">
                    <span class="ticker-sym">${symbol}</span>
                    <span class="ticker-badge ${exchange.toLowerCase()}-${segment.toLowerCase()}">${exchange}:${segment}</span>
                </div>
                <span class="ticker-price ${priceClass}" id="sidebar-price-${spec.replace(/:/g, '_')}">${lastPriceText}</span>
            </div>
        `;
    }).join("");
}

// Handle chart symbol selection
window.selectChartSymbol = function(symbolSpec) {
    state.chartSymbol = symbolSpec;
    if (!state.ticksData[symbolSpec]) {
        state.ticksData[symbolSpec] = [];
    }
    
    // Update chart header labels
    const parts = symbolSpec.split(":");
    document.getElementById("chart-symbol").innerText = parts[2];
    document.getElementById("chart-exchange").innerText = `${parts[0]}:${parts[1]}`;
    
    // Re-highlight active class in DOM
    const items = document.querySelectorAll(".ticker-item");
    items.forEach(el => el.classList.remove("active"));
    
    updateTickersSidebarList();
    drawRealTimeChart(symbolSpec);
};

