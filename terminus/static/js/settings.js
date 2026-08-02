/* Terminus — settings modal (appearance nav, connectors CRUD + test),
   the new-session modal, and the files/logs panel.
   Depends on core.js and sessions.js (TW namespace).
   File path: static/js/settings.js */

"use strict";

(function () {
    const {$} = TW;

    const JSON_HEADERS = {"Content-Type": "application/json"};

    async function postJSON(url, body, method = "POST") {
        return fetch(url, {method, headers: JSON_HEADERS, body: JSON.stringify(body)});
    }

    /* =====================================================================
       New Session modal
       ===================================================================== */
    async function loadConnectorsInto(selectEl) {
        selectEl.innerHTML = `<option value="">— Select connector —</option>`;
        try {
            const res = await fetch("/api/connectors");
            const data = await res.json();
            Object.keys(data.connectors || {}).forEach(name => {
                const opt = document.createElement("option");
                opt.value = name;
                opt.textContent = name;
                selectEl.appendChild(opt);
            });
        } catch (_) { /* ignore */ }
    }

    $("newSessionBtn").onclick = async () => {
        await loadConnectorsInto($("connectorSelect"));
        $("deviceHost").value = "";
        TW.openModal("newSessionModal");
    };
    $("closeNewSessionModal").onclick = () => TW.closeModal("newSessionModal");
    $("cancelNewSession").onclick = () => TW.closeModal("newSessionModal");

    $("newSessionForm").onsubmit = (e) => {
        e.preventDefault();
        const connector = $("connectorSelect").value;
        const raw = $("deviceHost").value;
        if (!connector || !raw.trim()) return;

        const hosts = [...new Set(raw.split("\n").map(h => h.trim()).filter(Boolean))];
        if (!hosts.length) return;
        TW.closeModal("newSessionModal");

        let firstId = null;
        hosts.forEach(hostname => {
            const id = TW.openSession(connector, hostname, false);
            if (!firstId) firstId = id;
        });
        if (firstId) TW.activate(firstId);
    };

    /* =====================================================================
       Settings modal + nav
       ===================================================================== */
    $("settingsBtn").onclick = () => {
        TW.openModal("settingsModal");
        switchSettingsPage("appearancePage");
    };
    $("closeSettingsModal").onclick = () => TW.closeModal("settingsModal");

    document.querySelectorAll(".settings-nav-item").forEach(item => {
        item.onclick = () => switchSettingsPage(item.dataset.page);
    });

    function switchSettingsPage(pageId) {
        let title = "Settings";
        document.querySelectorAll(".settings-nav-item").forEach(n => {
            const on = n.dataset.page === pageId;
            n.classList.toggle("active", on);
            if (on && n.dataset.title) title = n.dataset.title;
        });
        document.querySelectorAll(".settings-page").forEach(p =>
            p.classList.toggle("active", p.id === pageId));
        $("settingsPaneTitle").textContent = title;

        if (pageId === "appearancePage") {
            TW.renderThemeGrid();
            TW.renderFontControls();
        } else if (pageId === "connectorsPage") {
            loadConnectors();
        } else if (pageId === "filesPage") {
            loadLogs();
        }
    }

    /* =====================================================================
       Files (logs)
       ===================================================================== */
    const logsSearch = $("logsSearch");
    let allLogs = [];

    async function loadLogs() {
        try {
            const res = await fetch("/logs");
            if (!res.ok) return;
            allLogs = (await res.json()).logs || [];
            renderLogs();
        } catch (e) {
            console.error("loadLogs failed:", e);
        }
    }

    function filteredLogs() {
        const q = logsSearch.value.toLowerCase().trim();
        return allLogs.filter(f => f.filename.toLowerCase().includes(q));
    }

    function renderLogs() {
        const rows = filteredLogs();
        const body = $("logsBody");
        if (!rows.length) {
            body.innerHTML = `<tr><td colspan="3" class="table-empty">No matching logs</td></tr>`;
            return;
        }
        body.innerHTML = "";
        rows.forEach(f => {
            const tr = document.createElement("tr");
            tr.innerHTML = `
                <td class="fname" title="${f.filename}">${f.filename}</td>
                <td>${f.created_str}</td>
                <td class="row-actions">
                    <button class="btn btn--icon" data-act="view" title="View">
                        <span class="material-icons i-16">file_open</span></button>
                    <button class="btn btn--icon" data-act="download" title="Download">
                        <span class="material-icons i-16">download</span></button>
                    <button class="btn btn--icon btn--danger" data-act="delete" title="Delete">
                        <span class="material-icons i-16">delete</span></button>
                </td>`;
            tr.querySelector('[data-act="view"]').onclick = () => viewLog(f.filename);
            tr.querySelector('[data-act="download"]').onclick = () => downloadLogFile(f.filename);
            tr.querySelector('[data-act="delete"]').onclick = () => deleteLog(f.filename);
            body.appendChild(tr);
        });
    }

    async function viewLog(filename) {
        try {
            const res = await fetch(`/logs/view/${encodeURIComponent(filename)}`);
            if (!res.ok) return;
            $("viewLogTitle").textContent = filename;
            $("viewLogContent").textContent = await res.text();
            TW.openModal("viewLogModal");
        } catch (e) {
            console.error("viewLog failed:", e);
        }
    }

    async function downloadLogFile(filename) {
        try {
            const res = await fetch(`/logs/view/${encodeURIComponent(filename)}`);
            if (!res.ok) return;
            TW.triggerDownload(await res.blob(), filename);
        } catch (e) {
            console.error("downloadLogFile failed:", e);
        }
    }

    async function deleteLogsRequest(filenames) {
        try {
            const res = await postJSON("/logs", {filenames}, "DELETE");
            if (!res.ok) {
                TW.toast(await TW.errText(res, `Delete failed: ${res.status}`));
                return null;
            }
            return await res.json();
        } catch (e) {
            console.error("deleteLogsRequest failed:", e);
            return null;
        }
    }

    async function deleteLog(filename) {
        if (!confirm(`Delete log?\n\n${filename}`)) return;
        const result = await deleteLogsRequest([filename]);
        if (result) applyDeleteResult(result);
    }

    async function deleteAllLogs() {
        const targets = filteredLogs().map(f => f.filename);
        if (!targets.length) return;
        if (!confirm(`Delete ${targets.length} log file(s)?`)) return;
        const result = await deleteLogsRequest(targets);
        if (result) applyDeleteResult(result);
    }

    function applyDeleteResult(result) {
        const removed = new Set(result.deleted || []);
        allLogs = allLogs.filter(f => !removed.has(f.filename));
        renderLogs();
        if ((result.skipped || []).length) {
            TW.toast(`Skipped ${result.skipped.length} file(s) tied to an active session`);
        }
    }

    logsSearch.addEventListener("input", renderLogs);
    $("logsRefresh").onclick = loadLogs;
    $("logsDeleteAll").onclick = deleteAllLogs;
    $("closeViewLogModal").onclick = () => TW.closeModal("viewLogModal");

    // Called by sessions.js on session_ready / session_ended.
    TW.refreshLogsIfOpen = function () {
        if ($("settingsModal").classList.contains("open") &&
            $("filesPage").classList.contains("active")) {
            loadLogs();
        }
    };

    /* =====================================================================
       Connectors (table + Add/Edit modal + test connection)
       ===================================================================== */
    let connectors = {};   // name -> { jumphost: bool }

    async function loadConnectors() {
        try {
            const res = await fetch("/api/connectors");
            if (!res.ok) return;
            connectors = (await res.json()).connectors || {};
            renderConnectors();
        } catch (e) {
            console.error("loadConnectors failed:", e);
        }
    }

    function renderConnectors() {
        const body = $("connBody");
        const names = Object.keys(connectors);
        if (!names.length) {
            body.innerHTML = `<tr><td colspan="3" class="table-empty">No connectors yet</td></tr>`;
            return;
        }
        body.innerHTML = "";
        names.forEach(name => {
            const jump = !!connectors[name]?.jumphost;
            const label = jump ? "Via Jumphost" : "Direct connection";
            const tr = document.createElement("tr");
            tr.innerHTML = `
                <td class="cell-name">${name}</td>
                <td class="col-type">
                    <span class="type-icon ${jump ? "jump" : "direct"}" title="${label}">
                        <span class="material-icons">${jump ? "alt_route" : "tv"}</span>
                        <span>${label}</span>
                    </span>
                </td>
                <td class="row-actions">
                    <button class="btn btn--icon" data-act="edit" title="Edit">
                        <span class="material-icons i-16">edit</span></button>
                    <button class="btn btn--icon btn--danger" data-act="delete" title="Delete">
                        <span class="material-icons i-16">delete</span></button>
                </td>`;
            tr.querySelector('[data-act="edit"]').onclick = () => editConnector(name);
            tr.querySelector('[data-act="delete"]').onclick = () => deleteConnector(name);
            body.appendChild(tr);
        });
    }

    const CONN_FIELDS = [
        "connName", "connNetUser", "connNetPass",
        "connJumpIp", "connJumpUser", "connJumpPass", "connTestHost",
    ];

    function resetConnForm() {
        CONN_FIELDS.forEach(id => { $(id).value = ""; });
        $("connName").disabled = false;
        hideConnTestResult();
    }

    function openAddConnector() {
        resetConnForm();
        $("connModalTitle").textContent = "Add Connector";
        TW.openModal("connModal");
        setTimeout(() => $("connName").focus(), 50);
    }

    async function editConnector(name) {
        try {
            const res = await fetch(`/api/connectors/${encodeURIComponent(name)}`);
            if (!res.ok) return;
            const c = await res.json();
            resetConnForm();
            $("connModalTitle").textContent = `Edit “${name}”`;
            $("connName").value = name;
            $("connName").disabled = true;             // name is the primary key
            $("connNetUser").value = c.network_username || "";
            $("connJumpIp").value = c.jumphost_ip || "";
            $("connJumpUser").value = c.jumphost_username || "";
            TW.openModal("connModal");
        } catch (e) {
            console.error("editConnector failed:", e);
        }
    }

    function connFormPayload() {
        return {
            name: $("connName").value.trim(),
            network_username: $("connNetUser").value,
            network_password: $("connNetPass").value,
            jumphost_ip: $("connJumpIp").value.trim(),
            jumphost_username: $("connJumpUser").value,
            jumphost_password: $("connJumpPass").value,
        };
    }

    $("connAddBtn").onclick = openAddConnector;
    $("connRefresh").onclick = loadConnectors;
    $("closeConnModal").onclick = () => TW.closeModal("connModal");
    $("cancelConnModal").onclick = () => TW.closeModal("connModal");

    $("connForm").onsubmit = async (e) => {
        e.preventDefault();
        const payload = connFormPayload();
        if (!payload.name) {
            TW.toast("Connector name is required.");
            return;
        }
        try {
            const res = await postJSON("/api/connectors", payload);
            if (!res.ok) {
                TW.toast(await TW.errText(res, "Save failed."));
                return;
            }
            TW.toast(`Saved “${payload.name}”.`);
            TW.closeModal("connModal");
            await loadConnectors();
        } catch (e) {
            console.error("saveConnector failed:", e);
            TW.toast("Save failed.");
        }
    };

    async function deleteConnector(name) {
        if (!confirm(`Delete connector “${name}”?`)) return;
        try {
            const res = await fetch(
                `/api/connectors/${encodeURIComponent(name)}`, {method: "DELETE"}
            );
            if (!res.ok) {
                TW.toast(await TW.errText(res, "Delete failed."));
                return;
            }
            TW.toast(`Deleted “${name}”.`);
            await loadConnectors();
        } catch (e) {
            console.error("deleteConnector failed:", e);
            TW.toast("Delete failed.");
        }
    }

    /* ---- Test connection ---- */
    const TEST_ICONS = {ok: "check_circle", fail: "error", busy: "hourglass_top"};

    function showConnTestResult(kind, msg) {
        const el = $("connTestResult");
        el.className = `test-result show ${kind}`;
        el.innerHTML =
            `<span class="material-icons i-16">${TEST_ICONS[kind]}</span><span>${msg}</span>`;
    }

    function hideConnTestResult() {
        $("connTestResult").className = "test-result";
    }

    $("connTestBtn").onclick = async () => {
        const host = $("connTestHost").value.trim();
        if (!host) {
            TW.toast("Enter a host to test against.");
            return;
        }
        const payload = {...connFormPayload(), hostname: host};
        showConnTestResult("busy", `Connecting to ${host}…`);
        try {
            const res = await postJSON("/api/connectors/test", payload);
            const data = await res.json().catch(() => ({}));
            if (res.ok && data.ok) {
                showConnTestResult("ok", data.message || "Connection succeeded.");
            } else {
                showConnTestResult("fail", data.message || "Connection failed.");
            }
        } catch (e) {
            console.error("testConnection failed:", e);
            showConnTestResult("fail", "Request failed.");
        }
    };
})();