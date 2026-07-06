// Global Application State
const state = {
    adminToken: null,
    apiBase: window.location.origin
};

// Copies an input field's current value to the clipboard and briefly
// flashes the triggering button's label to confirm the copy succeeded.
// navigator.clipboard requires a secure context (HTTPS or localhost) - on
// a plain HTTP deployment that API is unavailable/silently rejects, so this
// falls back to the older execCommand("copy") approach, which works on
// insecure origins too.
function copyFieldToClipboard(inputId, buttonId, restoreLabel) {
    const input = document.getElementById(inputId);
    const value = input.value;
    if (!value) return;

    const flashCopied = () => {
        const btn = document.getElementById(buttonId);
        btn.innerText = "Copied!";
        setTimeout(() => { btn.innerText = restoreLabel; }, 2000);
    };

    if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(value).then(flashCopied).catch(() => fallbackCopy(value, flashCopied));
    } else {
        fallbackCopy(value, flashCopied);
    }
}

function fallbackCopy(value, onSuccess) {
    const textarea = document.createElement("textarea");
    textarea.value = value;
    textarea.style.position = "fixed";
    textarea.style.left = "-9999px";
    document.body.appendChild(textarea);
    textarea.focus();
    textarea.select();
    try {
        const ok = document.execCommand("copy");
        if (ok) onSuccess();
        else alert("Copy failed. Please select and copy the value manually.");
    } catch (e) {
        alert("Copy failed. Please select and copy the value manually.");
    } finally {
        document.body.removeChild(textarea);
    }
}

// Initialize Application: the console loads directly, no login step -
// an admin session token is acquired silently in the background.
document.addEventListener("DOMContentLoaded", () => {
    acquireAdminSession();
});

async function acquireAdminSession() {
    try {
        const res = await fetch(`${state.apiBase}/v1/auth/admin-login`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ username: "", password: "" })
        });

        if (!res.ok) throw new Error("Admin session request failed");

        const data = await res.json();
        state.adminToken = data.access_token;
    } catch (err) {
        console.error("Failed to acquire admin session:", err);
    }

    initApp();
}

// Runs once, after the admin has successfully logged in
function initApp() {
    initForms();
    checkApiHealth();
    refreshAdminKeys();
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
    // Admin Key Create Form
    document.getElementById("admin-key-form").addEventListener("submit", async (e) => {
        e.preventDefault();
        if (!state.adminToken) {
            alert("Please log in to the admin console first!");
            return;
        }

        const owner = document.getElementById("admin-owner").value.trim();
        const name = document.getElementById("admin-name").value.trim();
        const checkedScopes = Array.from(document.querySelectorAll('input[name="admin-scopes"]:checked')).map(cb => cb.value);
        const symbolsRaw = document.getElementById("admin-symbols").value.trim();
        const allowedSymbols = symbolsRaw ? symbolsRaw.split(",").map(s => s.trim().toUpperCase()).filter(Boolean) : [];
        const maxReplaySpeed = parseInt(document.getElementById("admin-max-speed").value);
        const rateLimit = parseInt(document.getElementById("admin-rate-limit").value);

        try {
            const res = await fetch(`${state.apiBase}/v1/admin/keys`, {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    "Authorization": `Bearer ${state.adminToken}`
                },
                body: JSON.stringify({
                    owner: owner,
                    name: name,
                    scopes: checkedScopes,
                    allowed_symbols: allowedSymbols,
                    max_replay_speed: maxReplaySpeed,
                    rate_limit_per_min: rateLimit
                })
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

            await refreshAdminKeys();

        } catch (err) {
            alert(err.message);
        }
    });

    // Admin Keys Table Refresh
    document.getElementById("btn-refresh-keys").addEventListener("click", refreshAdminKeys);

    // Edit Key Modal
    document.getElementById("btn-close-edit-modal").addEventListener("click", () => {
        document.getElementById("edit-key-modal").classList.add("hidden");
    });

    document.getElementById("edit-key-form").addEventListener("submit", async (e) => {
        e.preventDefault();
        if (!state.adminToken) return;

        const clientId = document.getElementById("edit-client-id").value;
        const name = document.getElementById("edit-name").value.trim();
        const checkedScopes = Array.from(document.querySelectorAll('input[name="edit-scopes"]:checked')).map(cb => cb.value);
        const symbolsRaw = document.getElementById("edit-symbols").value.trim();
        const allowedSymbols = symbolsRaw ? symbolsRaw.split(",").map(s => s.trim().toUpperCase()).filter(Boolean) : [];
        const maxReplaySpeed = parseInt(document.getElementById("edit-max-speed").value);
        const rateLimit = parseInt(document.getElementById("edit-rate-limit").value);

        try {
            const res = await fetch(`${state.apiBase}/v1/admin/keys/${clientId}`, {
                method: "PATCH",
                headers: {
                    "Content-Type": "application/json",
                    "Authorization": `Bearer ${state.adminToken}`
                },
                body: JSON.stringify({
                    name: name,
                    scopes: checkedScopes,
                    allowed_symbols: allowedSymbols,
                    max_replay_speed: maxReplaySpeed,
                    rate_limit_per_min: rateLimit
                })
            });

            if (!res.ok) {
                const err = await res.json();
                throw new Error(err.detail || "Failed to update key");
            }

            document.getElementById("edit-key-modal").classList.add("hidden");
            await refreshAdminKeys();
        } catch (err) {
            alert(err.message);
        }
    });

    // Secret Reveal Modal
    document.getElementById("btn-close-secret-modal").addEventListener("click", () => {
        document.getElementById("secret-reveal-modal").classList.add("hidden");
    });

    document.getElementById("btn-copy-reveal-id").addEventListener("click", (e) => {
        e.preventDefault();
        copyFieldToClipboard("reveal-client-id", "btn-copy-reveal-id", "Copy");
    });

    document.getElementById("btn-copy-reveal-secret").addEventListener("click", (e) => {
        e.preventDefault();
        copyFieldToClipboard("reveal-client-secret", "btn-copy-reveal-secret", "Copy");
    });

    // Copy buttons for generated keys
    document.getElementById("btn-copy-gen-id").addEventListener("click", (e) => {
        e.preventDefault();
        copyFieldToClipboard("generated-client-id", "btn-copy-gen-id", "Copy");
    });

    document.getElementById("btn-copy-gen-secret").addEventListener("click", (e) => {
        e.preventDefault();
        copyFieldToClipboard("generated-client-secret", "btn-copy-gen-secret", "Copy");
    });

    document.getElementById("btn-test-sandbox").addEventListener("click", (e) => {
        e.preventDefault();
        const clientId = document.getElementById("generated-client-id").value;
        const clientSecret = document.getElementById("generated-client-secret").value;
        window.open(`sandbox.html?client_id=${encodeURIComponent(clientId)}&client_secret=${encodeURIComponent(clientSecret)}`, '_blank');
    });

    document.getElementById("btn-test-reveal-sandbox").addEventListener("click", (e) => {
        e.preventDefault();
        const clientId = document.getElementById("reveal-client-id").value;
        const clientSecret = document.getElementById("reveal-client-secret").value;
        window.open(`sandbox.html?client_id=${encodeURIComponent(clientId)}&client_secret=${encodeURIComponent(clientSecret)}`, '_blank');
    });
}

// Admin Key Management
const adminKeysById = {}; // client_id -> key object, cached for the edit modal

async function refreshAdminKeys() {
    if (!state.adminToken) return;
    const tbody = document.getElementById("admin-keys-tbody");

    try {
        const res = await fetch(`${state.apiBase}/v1/admin/keys`, {
            headers: { "Authorization": `Bearer ${state.adminToken}` }
        });

        if (!res.ok) throw new Error("Failed to load API keys");

        const keys = await res.json();

        Object.keys(adminKeysById).forEach(k => delete adminKeysById[k]);
        keys.forEach(k => { adminKeysById[k.client_id] = k; });

        if (keys.length === 0) {
            tbody.innerHTML = `<tr><td colspan="7" class="empty-state">No API keys yet.</td></tr>`;
            return;
        }

        tbody.innerHTML = keys.map(renderKeyRow).join("");
    } catch (err) {
        tbody.innerHTML = `<tr><td colspan="7" class="empty-state">Failed to load API keys.</td></tr>`;
    }
}

function renderKeyRow(key) {
    const scopeChips = (key.scopes || []).map(s => `<span class="scope-chip">${s}</span>`).join("");
    const symbolsText = (key.allowed_symbols && key.allowed_symbols.length > 0)
        ? key.allowed_symbols.join(", ")
        : "<em>All symbols</em>";

    const pauseResumeBtn = key.status === "paused"
        ? `<button class="btn btn-sm secondary-btn" onclick="adminKeyAction('${key.client_id}', 'resume')">Resume</button>`
        : `<button class="btn btn-sm secondary-btn" onclick="adminKeyAction('${key.client_id}', 'pause')" ${key.status === 'disabled' ? 'disabled' : ''}>Pause</button>`;

    const disableBtn = key.status === "disabled"
        ? `<button class="btn btn-sm secondary-btn" onclick="adminKeyAction('${key.client_id}', 'resume')">Enable</button>`
        : `<button class="btn btn-sm secondary-btn" onclick="adminKeyAction('${key.client_id}', 'disable')">Disable</button>`;

    return `
        <tr>
            <td class="mono">${key.client_id}</td>
            <td>${key.owner}${key.name ? `<br><small class="helper-text">${key.name}</small>` : ""}</td>
            <td>${scopeChips}</td>
            <td>${symbolsText}</td>
            <td>${key.max_replay_speed}x</td>
            <td><span class="status-pill ${key.status}">${key.status}</span></td>
            <td class="key-actions">
                <button class="btn btn-sm secondary-btn" style="color: #6366f1; border-color: rgba(99, 102, 241, 0.4);" onclick="testKeyInSandbox('${key.client_id}')">Test</button>
                <button class="btn btn-sm secondary-btn" onclick="openEditKeyModal('${key.client_id}')">Edit</button>
                <button class="btn btn-sm secondary-btn" onclick="adminRegenerateSecret('${key.client_id}')">Regenerate Secret</button>
                ${pauseResumeBtn}
                ${disableBtn}
                <button class="btn btn-sm secondary-btn" style="color: var(--accent-danger);" onclick="adminDeleteKey('${key.client_id}')">Delete</button>
            </td>
        </tr>
    `;
}

window.testKeyInSandbox = function(clientId) {
    window.open(`sandbox.html?client_id=${encodeURIComponent(clientId)}`, '_blank');
};

window.openEditKeyModal = function(clientId) {
    const key = adminKeysById[clientId];
    if (!key) return;

    document.getElementById("edit-client-id").value = key.client_id;
    document.getElementById("edit-name").value = key.name || "";
    document.getElementById("edit-symbols").value = (key.allowed_symbols || []).join(", ");
    document.getElementById("edit-max-speed").value = String(key.max_replay_speed);
    document.getElementById("edit-rate-limit").value = key.rate_limit_per_min;

    const scopeSet = new Set(key.scopes || []);
    document.querySelectorAll('input[name="edit-scopes"]').forEach(cb => {
        cb.checked = scopeSet.has(cb.value);
    });

    document.getElementById("edit-key-modal").classList.remove("hidden");
};

window.adminRegenerateSecret = async function(clientId) {
    if (!state.adminToken) return;
    if (!confirm(`Regenerate the secret for '${clientId}'? The old secret will stop working immediately.`)) return;

    try {
        const res = await fetch(`${state.apiBase}/v1/admin/keys/${clientId}/regenerate-secret`, {
            method: "POST",
            headers: { "Authorization": `Bearer ${state.adminToken}` }
        });

        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || "Failed to regenerate secret");
        }

        const data = await res.json();

        document.getElementById("reveal-client-id").value = data.client_id;
        document.getElementById("reveal-client-secret").value = data.client_secret;
        document.getElementById("secret-reveal-modal").classList.remove("hidden");
    } catch (err) {
        alert(err.message);
    }
};

window.adminKeyAction = async function(clientId, action) {
    if (!state.adminToken) return;

    try {
        const res = await fetch(`${state.apiBase}/v1/admin/keys/${clientId}/${action}`, {
            method: "POST",
            headers: { "Authorization": `Bearer ${state.adminToken}` }
        });

        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || `Failed to ${action} key`);
        }

        await refreshAdminKeys();
    } catch (err) {
        alert(err.message);
    }
};

window.adminDeleteKey = async function(clientId) {
    if (!state.adminToken) return;
    if (!confirm(`Delete API key '${clientId}'? This cannot be undone from the console.`)) return;

    try {
        const res = await fetch(`${state.apiBase}/v1/admin/keys/${clientId}`, {
            method: "DELETE",
            headers: { "Authorization": `Bearer ${state.adminToken}` }
        });

        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || "Failed to delete key");
        }

        await refreshAdminKeys();
    } catch (err) {
        alert(err.message);
    }
};
