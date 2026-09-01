/* Terminus — New Session modal, Settings modal (appearance, connectors, AI),
   and the Logs modal.

   The AI settings *page* is populated by ai.settings.js; this file only routes
   to it. Depends on core.js and sessions.js (TW namespace).

   File path: terminus/static/js/settings.js */

"use strict";

(function () {
    const {$} = TW;

    const FOCUS_DELAY_MS = 50;

    const TEST_ICON = {
        ok: "check_circle",
        fail: "error",
        busy: "hourglass_top",
    };

    function plural(count, noun) {
        return `${count} ${noun}${count === 1 ? "" : "s"}`;
    }

    /* Shared shape for the two data tables. */
    function emptyRow(columns, message) {
        return `<tr><td colspan="${columns}" class="table-empty">` +
            `${TW.esc(message)}</td></tr>`;
    }


    /* =====================================================================
       New Session modal
       ===================================================================== */
    async function loadConnectorsInto(select) {
        select.innerHTML = `<option value="">— Select connector —</option>`;
        try {
            const res = await fetch("/api/connectors");
            if (!res.ok) return;
            const data = await res.json();
            Object.keys(data.connectors || {}).forEach((name) => {
                const option = document.createElement("option");
                option.value = name;
                option.textContent = name;      // textContent — never innerHTML
                select.appendChild(option);
            });
        } catch (err) {
            console.error("loadConnectorsInto failed:", err);
        }
    }

    $("newSessionBtn").onclick = async () => {
        await Promise.all([
            loadConnectorsInto($("connectorSelect")),
            loadShells(),
        ]);
        $("deviceHost").value = "";
        TW.openModal("newSessionModal");
    };

    $("closeNewSessionModal").onclick = () => TW.closeModal("newSessionModal");
    $("cancelNewSession").onclick = () => TW.closeModal("newSessionModal");

    /* One line per host, so a pasted list opens a session each. Duplicates are
       dropped — opening the same device twice by accident is never intended. */
    $("newSessionForm").onsubmit = (e) => {
        e.preventDefault();

        const connector = $("connectorSelect").value;
        const raw = $("deviceHost").value;
        if (!connector || !raw.trim()) return;

        const hosts = [...new Set(
            raw.split("\n").map((host) => host.trim()).filter(Boolean),
        )];
        if (!hosts.length) return;

        TW.closeModal("newSessionModal");

        // Open them all, then activate the first — otherwise each new terminal
        // steals focus and only the last is visible.
        let firstId = null;
        hosts.forEach((hostname) => {
            const id = TW.openSession(connector, hostname, false);
            if (!firstId) firstId = id;
        });
        if (firstId) TW.activate(firstId);
    };


    /* =====================================================================
       Local shell split button
       ===================================================================== */
    let shells = [];

    function currentShell() {
        const saved = localStorage.getItem(TW.SHELL_KEY);
        return shells.find((shell) => shell.id === saved) || shells[0] || null;
    }

    function renderShellMenu() {
        const menu = $("shellMenu");
        const active = currentShell();

        $("shellLabel").textContent = active ? active.label : "Local Shell";
        $("openShellBtn").disabled = !active;
        $("shellMenuBtn").disabled = shells.length < 2;

        menu.innerHTML = "";
        shells.forEach((shell) => {
            const isActive = active && shell.id === active.id;
            const item = document.createElement("div");
            item.className = "split-menu-item" + (isActive ? " active" : "");
            item.setAttribute("role", "menuitem");
            item.tabIndex = 0;
            item.innerHTML = `
                <span>${TW.esc(shell.label)}</span>
                <span class="material-icons i-16 check"
                      aria-hidden="true">check</span>`;

            const choose = () => {
                localStorage.setItem(TW.SHELL_KEY, shell.id);
                closeShellMenu();
                renderShellMenu();
                launchShell();          // picking a shell also opens it
            };

            item.onclick = choose;
            item.onkeydown = (e) => {
                if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    choose();
                }
            };

            menu.appendChild(item);
        });
    }

    async function loadShells() {
        try {
            const res = await fetch("/api/shells");
            if (!res.ok) return;
            shells = (await res.json()).shells || [];
        } catch (err) {
            console.error("loadShells failed:", err);
            shells = [];
        }
        renderShellMenu();
    }

    function launchShell() {
        const shell = currentShell();
        if (!shell) return;
        TW.closeModal("newSessionModal");
        TW.openShell(shell.id);
    }

    function closeShellMenu() {
        $("shellMenu").classList.remove("open");
        $("shellMenuBtn").setAttribute("aria-expanded", "false");
    }

    $("openShellBtn").onclick = launchShell;

    $("shellMenuBtn").onclick = (e) => {
        e.stopPropagation();
        const open = $("shellMenu").classList.toggle("open");
        $("shellMenuBtn").setAttribute("aria-expanded", open ? "true" : "false");
    };

    document.addEventListener("click", (e) => {
        if (!$("shellSplit").contains(e.target)) closeShellMenu();
    });

    document.addEventListener("keydown", (e) => {
        if (e.key === "Escape" && $("shellMenu").classList.contains("open")) {
            // Close the menu, not the modal behind it.
            e.stopPropagation();
            closeShellMenu();
            $("shellMenuBtn").focus();
        }
    }, true);       // capture, so this runs before core.js's modal handler


    /* =====================================================================
       Settings modal
       ===================================================================== */
    $("settingsBtn").onclick = () => {
        TW.openModal("settingsModal");
        switchSettingsPage("appearancePage");
    };

    $("closeSettingsModal").onclick = () => TW.closeModal("settingsModal");

    function switchSettingsPage(pageId) {
        let title = "Settings";

        document.querySelectorAll(".settings-nav-item").forEach((item) => {
            const selected = item.dataset.page === pageId;
            item.classList.toggle("active", selected);
            item.setAttribute("aria-selected", selected ? "true" : "false");
            if (selected && item.dataset.title) title = item.dataset.title;
        });

        document.querySelectorAll(".settings-page").forEach((page) => {
            page.classList.toggle("active", page.id === pageId);
        });

        $("settingsPaneTitle").textContent = title;

        // Each page loads lazily — connectors and AI settings both hit the API.
        if (pageId === "appearancePage") {
            TW.renderThemeGrid();
            TW.renderAppearanceControls();
        } else if (pageId === "connectorsPage") {
            loadConnectors();
        } else if (pageId === "aiPage") {
            TW.loadAISettings?.();
        }
    }

    const navItems = [...document.querySelectorAll(".settings-nav-item")];

    navItems.forEach((item, index) => {
        item.onclick = () => switchSettingsPage(item.dataset.page);

        item.onkeydown = (e) => {
            if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                switchSettingsPage(item.dataset.page);
                return;
            }
            if (e.key !== "ArrowUp" && e.key !== "ArrowDown") return;

            // Vertical tablist, so skip anything hidden (the AI page when the
            // server has no ai package).
            const visible = navItems.filter((n) => n.offsetParent !== null);
            const position = visible.indexOf(item);
            const step = e.key === "ArrowDown" ? 1 : -1;
            const next = visible[
                (position + step + visible.length) % visible.length];
            if (!next) return;
            e.preventDefault();
            next.focus();
            switchSettingsPage(next.dataset.page);
        };
    });


    /* =====================================================================
       Logs modal
       ===================================================================== */
    let allLogs = [];

    async function loadLogs() {
        try {
            const res = await fetch("/logs");
            if (!res.ok) return;
            allLogs = (await res.json()).logs || [];
            renderLogs();
        } catch (err) {
            console.error("loadLogs failed:", err);
        }
    }

    function filteredLogs() {
        const query = $("logsSearch").value.toLowerCase().trim();
        return allLogs.filter(
            (file) => file.filename.toLowerCase().includes(query));
    }

    function renderLogs() {
        const rows = filteredLogs();
        const body = $("logsBody");

        if (!rows.length) {
            body.innerHTML = emptyRow(3,
                allLogs.length ? "No matching logs" : "No logs yet");
            $("logsFootHint").textContent = "";
            return;
        }

        body.innerHTML = "";
        rows.forEach((file) => {
            const safeName = TW.esc(file.filename);
            const tr = document.createElement("tr");
            tr.innerHTML = `
                <td class="fname" title="${safeName}">${safeName}</td>
                <td>${TW.esc(file.created_str)}</td>
                <td class="row-actions">
                    <button class="btn btn--icon" data-act="view"
                            title="View" aria-label="View log">
                        <span class="material-icons i-16"
                              aria-hidden="true">file_open</span>
                    </button>
                    <button class="btn btn--icon" data-act="open"
                            title="Open with default app"
                            aria-label="Open with default app">
                        <span class="material-icons i-16"
                              aria-hidden="true">open_in_new</span>
                    </button>
                    <button class="btn btn--icon btn--danger" data-act="delete"
                            title="Delete" aria-label="Delete log">
                        <span class="material-icons i-16"
                              aria-hidden="true">delete</span>
                    </button>
                </td>`;

            tr.querySelector('[data-act="view"]').onclick =
                () => viewLog(file.filename);
            tr.querySelector('[data-act="open"]').onclick =
                () => openLogFile(file.filename);
            tr.querySelector('[data-act="delete"]').onclick =
                () => deleteLog(file.filename);

            body.appendChild(tr);
        });

        $("logsFootHint").textContent = plural(rows.length, "log file");
    }

    async function viewLog(filename) {
        try {
            const res = await fetch(`/logs/view/${encodeURIComponent(filename)}`);
            if (!res.ok) {
                TW.toast(await TW.errText(res, "Could not read the log."));
                return;
            }
            $("viewLogTitle").textContent = filename;
            // textContent: log bodies contain raw device output.
            $("viewLogContent").textContent = await res.text();
            TW.openModal("viewLogModal");
        } catch (err) {
            console.error("viewLog failed:", err);
            TW.toast("Could not read the log.");
        }
    }

    /* Hands the file to the OS — the server and the user are the same machine. */
    async function openLogFile(filename) {
        try {
            const res = await TW.postJSON(
                `/api/open/log/${encodeURIComponent(filename)}`);
            const data = await res.json().catch(() => ({}));
            if (!res.ok || !data.ok) {
                TW.toast(data.message || "Could not open the file.");
            }
        } catch (err) {
            console.error("openLogFile failed:", err);
            TW.toast("Could not open the file.");
        }
    }

    async function openLogFolder() {
        try {
            const res = await TW.postJSON("/api/open/folder");
            const data = await res.json().catch(() => ({}));
            if (!res.ok || !data.ok) {
                TW.toast(data.message || "Could not open the folder.");
            }
        } catch (err) {
            console.error("openLogFolder failed:", err);
            TW.toast("Could not open the folder.");
        }
    }

    async function deleteLogsRequest(filenames) {
        try {
            const res = await TW.postJSON("/logs", {filenames}, "DELETE");
            if (!res.ok) {
                TW.toast(await TW.errText(res, `Delete failed: ${res.status}`));
                return null;
            }
            return await res.json();
        } catch (err) {
            console.error("deleteLogsRequest failed:", err);
            TW.toast("Delete failed.");
            return null;
        }
    }

    async function deleteLog(filename) {
        if (!confirm(`Delete log?\n\n${filename}`)) return;
        const result = await deleteLogsRequest([filename]);
        if (result) applyDeleteResult(result);
    }

    /* Deletes what is currently *shown*, so a filter narrows the blast radius. */
    async function deleteAllLogs() {
        const targets = filteredLogs().map((file) => file.filename);
        if (!targets.length) return;
        if (!confirm(`Delete ${plural(targets.length, "log file")}?`)) return;

        const result = await deleteLogsRequest(targets);
        if (result) applyDeleteResult(result);
    }

    function applyDeleteResult(result) {
        const removed = new Set(result.deleted || []);
        allLogs = allLogs.filter((file) => !removed.has(file.filename));
        renderLogs();

        // The server refuses to delete a log belonging to a live session.
        const skipped = (result.skipped || []).length;
        if (skipped) {
            TW.toast(`Skipped ${plural(skipped, "file")} ` +
                `tied to an active session.`);
        }
        const errors = (result.errors || []).length;
        if (errors) {
            TW.toast(`Could not delete ${plural(errors, "file")}.`);
        }
    }

    $("logsBtn").onclick = () => {
        TW.openModal("logsModal");
        loadLogs();
    };

    $("closeLogsModal").onclick = () => TW.closeModal("logsModal");
    $("closeViewLogModal").onclick = () => TW.closeModal("viewLogModal");
    $("logsSearch").addEventListener("input", renderLogs);
    $("logsOpenFolder").onclick = openLogFolder;
    $("logsRefresh").onclick = loadLogs;
    $("logsDeleteAll").onclick = deleteAllLogs;

    /* Called by sessions.js on session_ready / session_ended, so a new log
       appears without a manual refresh. */
    TW.refreshLogsIfOpen = function () {
        if ($("logsModal").classList.contains("open")) loadLogs();
    };


    /* =====================================================================
       Connectors — table
       ===================================================================== */
    let connectors = {};        // name -> {jumphost: bool}

    async function loadConnectors() {
        try {
            const res = await fetch("/api/connectors");
            if (!res.ok) return;
            connectors = (await res.json()).connectors || {};
            renderConnectors();
        } catch (err) {
            console.error("loadConnectors failed:", err);
        }
    }

    function filteredConnectors() {
        const query = $("connSearch").value.toLowerCase().trim();
        return Object.keys(connectors)
            .filter((name) => name.toLowerCase().includes(query));
    }

    function renderConnectors() {
        const body = $("connBody");
        const names = filteredConnectors();

        if (!names.length) {
            const filtering = $("connSearch").value.trim().length > 0;
            body.innerHTML = emptyRow(3,
                filtering ? "No matching connectors" : "No connectors yet");
            return;
        }

        body.innerHTML = "";
        names.forEach((name) => {
            const viaJumphost = !!connectors[name]?.jumphost;
            const label = viaJumphost ? "Via Jumphost" : "Direct connection";
            const safeName = TW.esc(name);

            const tr = document.createElement("tr");
            tr.innerHTML = `
                <td class="cell-name">${safeName}</td>
                <td class="col-type">
                    <span class="type-icon ${viaJumphost ? "jump" : "direct"}"
                          title="${label}">
                        <span class="material-icons" aria-hidden="true">${
                            viaJumphost ? "alt_route" : "tv"}</span>
                        <span>${label}</span>
                    </span>
                </td>
                <td class="row-actions">
                    <button class="btn btn--icon" data-act="edit"
                            title="Edit" aria-label="Edit ${safeName}">
                        <span class="material-icons i-16"
                              aria-hidden="true">edit</span>
                    </button>
                    <button class="btn btn--icon btn--danger" data-act="delete"
                            title="Delete" aria-label="Delete ${safeName}">
                        <span class="material-icons i-16"
                              aria-hidden="true">delete</span>
                    </button>
                </td>`;

            tr.querySelector('[data-act="edit"]').onclick =
                () => editConnector(name);
            tr.querySelector('[data-act="delete"]').onclick =
                () => deleteConnector(name);

            body.appendChild(tr);
        });
    }


    /* =====================================================================
       Connectors — add / edit form
       ===================================================================== */
    const CONN_FIELDS = [
        "connName", "connNetUser", "connNetPass",
        "connJumpIp", "connJumpUser", "connJumpPass",
        "connDeviceType", "connSshOptions", "connTestHost",
    ];

    function resetConnForm() {
        CONN_FIELDS.forEach((id) => {
            $(id).value = "";
        });
        $("connName").disabled = false;
        hideTestResult();
    }

    function connFormPayload() {
        return {
            name: $("connName").value.trim(),
            network_username: $("connNetUser").value,
            network_password: $("connNetPass").value,
            jumphost_ip: $("connJumpIp").value.trim(),
            jumphost_username: $("connJumpUser").value,
            jumphost_password: $("connJumpPass").value,
            device_type: $("connDeviceType").value,
            ssh_options: $("connSshOptions").value.trim(),
        };
    }

    function openAddConnector() {
        resetConnForm();
        $("connModalTitle").textContent = "Add Connector";
        TW.openModal("connModal");
        setTimeout(() => $("connName").focus(), FOCUS_DELAY_MS);
    }

    /* Passwords are never sent to the browser, so their fields stay blank —
       submitting blank keeps whatever is stored. */
    async function editConnector(name) {
        try {
            const res = await fetch(
                `/api/connectors/${encodeURIComponent(name)}`);
            if (!res.ok) {
                TW.toast(await TW.errText(res, "Could not load the connector."));
                return;
            }
            const connector = await res.json();

            resetConnForm();
            $("connModalTitle").textContent = `Edit “${name}”`;
            $("connName").value = name;
            $("connName").disabled = true;          // name is the primary key
            $("connNetUser").value = connector.network_username || "";
            $("connJumpIp").value = connector.jumphost_ip || "";
            $("connJumpUser").value = connector.jumphost_username || "";
            $("connDeviceType").value = connector.device_type || "";
            $("connSshOptions").value = connector.ssh_options || "";

            TW.openModal("connModal");
        } catch (err) {
            console.error("editConnector failed:", err);
            TW.toast("Could not load the connector.");
        }
    }

    async function deleteConnector(name) {
        if (!confirm(`Delete connector “${name}”?`)) return;
        try {
            const res = await fetch(
                `/api/connectors/${encodeURIComponent(name)}`,
                {method: "DELETE"});
            if (!res.ok) {
                TW.toast(await TW.errText(res, "Delete failed."));
                return;
            }
            TW.toast(`Deleted “${name}”.`);
            await loadConnectors();
        } catch (err) {
            console.error("deleteConnector failed:", err);
            TW.toast("Delete failed.");
        }
    }

    $("connSearch").addEventListener("input", renderConnectors);
    $("connAddBtn").onclick = openAddConnector;
    $("connRefresh").onclick = loadConnectors;
    $("closeConnModal").onclick = () => TW.closeModal("connModal");
    $("cancelConnModal").onclick = () => TW.closeModal("connModal");

    $("connForm").onsubmit = async (e) => {
        e.preventDefault();

        const payload = connFormPayload();
        if (!payload.name) {
            TW.toast("Connector name is required.");
            $("connName").focus();
            return;
        }

        try {
            const res = await TW.postJSON("/api/connectors", payload);
            if (!res.ok) {
                TW.toast(await TW.errText(res, "Save failed."));
                return;
            }
            TW.toast(`Saved “${payload.name}”.`);
            TW.closeModal("connModal");
            await loadConnectors();
        } catch (err) {
            console.error("saveConnector failed:", err);
            TW.toast("Save failed.");
        }
    };


    /* =====================================================================
       Connectors — test connection

       Opens a real SSH session and closes it again, so a failure message is
       the actual Netmiko/Paramiko error.
       ===================================================================== */
    function showTestResult(kind, message) {
        const el = $("connTestResult");
        el.className = `test-result show ${kind}`;
        // The message is a server-side exception string — escape it.
        el.innerHTML =
            `<span class="material-icons i-16" aria-hidden="true">${
                TEST_ICON[kind]}</span><span>${TW.esc(message)}</span>`;
    }

    function hideTestResult() {
        $("connTestResult").className = "test-result";
    }

    $("connTestBtn").onclick = async () => {
        const host = $("connTestHost").value.trim();
        if (!host) {
            TW.toast("Enter a host to test against.");
            $("connTestHost").focus();
            return;
        }

        showTestResult("busy", `Connecting to ${host}…`);
        try {
            const res = await TW.postJSON("/api/connectors/test",
                {...connFormPayload(), hostname: host});
            const data = await res.json().catch(() => ({}));
            showTestResult(
                res.ok && data.ok ? "ok" : "fail",
                data.message || (res.ok ? "Connection failed."
                    : `Request failed: ${res.status}`),
            );
        } catch (err) {
            console.error("testConnection failed:", err);
            showTestResult("fail", "Request failed.");
        }
    };
})();