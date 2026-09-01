/* Terminus — Tools modal: shared session selection, tabs, Broadcast.

   The selection model is deliberately shared with the Assistant tab
   (ai.chat.js): pick your devices once, then either push commands manually or
   ask questions about them.

   Depends on core.js and sessions.js (TW namespace).

   File path: terminus/static/js/tools.js */

"use strict";

(function () {
    const {$, socket, open} = TW;

    const TAB_BROADCAST = "tlBroadcast";
    const TAB_ASSISTANT = "tlAssistant";

    /* Long enough for the panel to become visible before we focus into it. */
    const FOCUS_DELAY_MS = 60;

    /* Above this, listing names is noise — show a count instead. */
    const MAX_NAMED_SCOPE = 3;


    /* =====================================================================
       Shared selection model
       ===================================================================== */
    const TL = TW.tools = {
        selected: new Set(),        // session ids
        tab: TAB_BROADCAST,
        open: false,
    };

    /* Only active sessions are selectable: a connecting or ended session has no
       usable channel, and the agent aliases the list in order, so a dead id
       would shift every alias. */
    function activeSessions() {
        return Object.entries(open)
            .filter(([, session]) => session.status === "active")
            .map(([id, session]) => ({id, name: session.name}));
    }

    function filtered() {
        const query = $("tlFilter").value.toLowerCase().trim();
        return activeSessions()
            .filter((s) => s.name.toLowerCase().includes(query));
    }

    TL.selectedIds = () => [...TL.selected];

    TL.selectedNames = () =>
        [...TL.selected].map((id) => open[id]?.name).filter(Boolean);

    /* Drop selections whose session has closed or gone idle. */
    function prune() {
        const live = new Set(activeSessions().map((s) => s.id));
        TL.selected = new Set([...TL.selected].filter((id) => live.has(id)));
    }


    /* =====================================================================
       Session picker
       ===================================================================== */
    function renderList() {
        const list = $("tlList");
        const rows = filtered();

        if (!rows.length) {
            const anyActive = activeSessions().length > 0;
            list.innerHTML = `<div class="tl-empty">${
                anyActive ? "No matching sessions" : "No active sessions"
            }</div>`;
            updateCount();
            return;
        }

        list.innerHTML = "";
        rows.forEach((session) => {
            const safeName = TW.esc(session.name);
            const row = document.createElement("label");
            row.className = "tl-item";

            // session.name is the device prompt — escape it.
            row.innerHTML = `
                <input type="checkbox"
                       ${TL.selected.has(session.id) ? "checked" : ""}/>
                <span class="circle active" aria-hidden="true"></span>
                <span class="tl-item-name" title="${safeName}">${safeName}</span>`;

            const checkbox = row.querySelector("input");
            checkbox.onchange = () => {
                if (checkbox.checked) TL.selected.add(session.id);
                else TL.selected.delete(session.id);
                updateCount();
            };

            list.appendChild(row);
        });

        updateCount();
    }

    function scopeText() {
        const count = TL.selected.size;
        if (count === 0) return "No sessions selected";
        if (count <= MAX_NAMED_SCOPE) return TL.selectedNames().join(", ");
        return `${count} sessions selected`;
    }

    function updateCount() {
        prune();

        const count = TL.selected.size;
        const scope = scopeText();

        $("tlCount").textContent = `${count} selected`;
        $("bcScope").textContent = count === 0 ? scope : `Sending to: ${scope}`;
        $("chatScope").textContent = count === 0 ? scope : `Asking about: ${scope}`;

        syncSelectAll();
        updateBroadcastStatus();
        TW.onSelectionChange?.();
    }

    TL.renderList = renderList;
    TL.updateCount = updateCount;

    function syncSelectAll() {
        const checkbox = $("tlSelectAll");
        const visible = filtered().map((s) => s.id);
        const chosen = visible.filter((id) => TL.selected.has(id)).length;

        checkbox.disabled = visible.length === 0;
        checkbox.checked = visible.length > 0 && chosen === visible.length;
        checkbox.indeterminate = chosen > 0 && chosen < visible.length;
    }

    /* Applies to the *filtered* rows only, so a filter plus select-all is a
       useful way to target a subset. */
    function toggleSelectAll() {
        const visible = filtered().map((s) => s.id);
        const selectAll = $("tlSelectAll").checked;
        visible.forEach((id) => {
            if (selectAll) TL.selected.add(id);
            else TL.selected.delete(id);
        });
        renderList();
    }


    /* =====================================================================
       Tabs
       ===================================================================== */
    function switchTab(tabId) {
        if (tabId === TAB_ASSISTANT && !TW.aiActive) return;

        TL.tab = tabId;

        document.querySelectorAll(".tl-tab").forEach((tab) => {
            const selected = tab.dataset.tab === tabId;
            tab.classList.toggle("active", selected);
            tab.setAttribute("aria-selected", selected ? "true" : "false");
        });

        document.querySelectorAll(".tl-panel").forEach((panel) => {
            panel.classList.toggle("active", panel.id === tabId);
        });

        setTimeout(() => {
            const target = tabId === TAB_BROADCAST ? "bcCommands" : "chatInput";
            $(target)?.focus();
        }, FOCUS_DELAY_MS);

        TW.onTabChange?.(tabId);
    }

    TL.switchTab = switchTab;

    document.querySelectorAll(".tl-tab").forEach((tab) => {
        tab.onclick = () => switchTab(tab.dataset.tab);

        // Arrow-key navigation, per the ARIA tabs pattern.
        tab.addEventListener("keydown", (e) => {
            if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
            const tabs = [...document.querySelectorAll(".tl-tab")]
                .filter((t) => t.offsetParent !== null);
            const index = tabs.indexOf(tab);
            const step = e.key === "ArrowRight" ? 1 : -1;
            const next = tabs[(index + step + tabs.length) % tabs.length];
            if (!next) return;
            e.preventDefault();
            next.focus();
            switchTab(next.dataset.tab);
        });
    });


    /* =====================================================================
       Open / close
       ===================================================================== */
    TL.openModal = function (tab = TAB_BROADCAST, preselect = null) {
        const sessions = activeSessions();
        if (!sessions.length) {
            TW.toast("No active sessions yet — open one first.");
            return;
        }

        if (preselect) {
            TL.selected = new Set(
                Array.isArray(preselect) ? preselect : [preselect]);
        } else if (!TL.selected.size) {
            // Select everything on first open — the common case.
            TL.selected = new Set(sessions.map((s) => s.id));
        }

        $("tlFilter").value = "";
        renderList();
        switchTab(TW.aiActive ? tab : TAB_BROADCAST);
        TW.openModal("toolsModal");
        TL.open = true;
    };

    /* Teardown any close path must run: notably, ai.chat.js cancels a pending
       approval here, otherwise the server sits blocked in _await_decision for
       its full timeout and every later question is refused as "already in
       progress". */
    function afterClose() {
        if (!TL.open) return;
        TL.open = false;
        TW.onToolsClose?.();
    }

    function closeModal() {
        TW.closeModal("toolsModal");
        afterClose();
    }

    TL.close = closeModal;

    $("toolsBtn").onclick = () => TL.openModal(TAB_BROADCAST);
    $("closeTools").onclick = closeModal;
    $("tlSelectAll").onchange = toggleSelectAll;
    $("tlFilter").addEventListener("input", renderList);

    /* Backdrop click and Escape are handled generically in core.js, which does
       not know about our teardown — observe the class instead of trying to
       intercept every close path. */
    new MutationObserver(() => {
        if (TL.open && !$("toolsModal").classList.contains("open")) afterClose();
    }).observe($("toolsModal"), {
        attributes: true,
        attributeFilter: ["class"],
    });

    // Keep the roster current while the panel is open.
    TW.onSessionsChanged = function () {
        if (TL.open) renderList();
    };


    /* =====================================================================
       Broadcast — status bar
       ===================================================================== */
    const STATUS_ICON = {
        idle: "edit_note",
        ok: "check_circle",
        error: "error",
    };

    function commandLines() {
        return $("bcCommands").value
            .replace(/\r\n/g, "\n")
            .split("\n")
            .filter((line) => line.trim());
    }

    function setBroadcastStatus(text, kind = "idle") {
        const bar = $("bcStatus");
        bar.className = `tl-status tl-status--${kind}`;
        $("bcStatusText").textContent = text;

        const icon = bar.querySelector(".tl-status-icon");
        if (icon) icon.textContent = STATUS_ICON[kind] || STATUS_ICON.idle;
    }

    function updateBroadcastStatus() {
        const commands = commandLines().length;
        const sessions = TL.selected.size;

        if (commands === 0) {
            setBroadcastStatus("No commands entered", "idle");
        } else if (sessions === 0) {
            setBroadcastStatus(
                `${plural(commands, "command")} — no sessions selected`, "error");
        } else {
            setBroadcastStatus(
                `${plural(commands, "command")} → ` +
                `${plural(sessions, "session")}`, "idle");
        }
    }

    TL.updateBroadcastStatus = updateBroadcastStatus;

    function plural(count, noun) {
        return `${count} ${noun}${count === 1 ? "" : "s"}`;
    }


    /* =====================================================================
       Broadcast — send

       No policy check and no approval: this is the user typing directly, which
       is exactly why the Assistant is restricted to read-only commands and
       this is not.
       ===================================================================== */
    function sendBroadcast() {
        const text = $("bcCommands").value;

        if (!text.trim()) {
            setBroadcastStatus("Enter at least one command.", "error");
            $("bcCommands").focus();
            return;
        }
        if (!TL.selected.size) {
            setBroadcastStatus("Select at least one session.", "error");
            return;
        }

        // CR-terminated lines. Blank lines are preserved so a trailing newline
        // still submits the final command, and a deliberate blank line still
        // sends a bare Enter.
        const lines = text.replace(/\r\n/g, "\n").split("\n");
        const payload = lines.join("\r") + "\r";

        let sent = 0;
        [...TL.selected].forEach((id) => {
            if (open[id]?.status !== "active") return;
            socket.emit("input", {session_id: id, data: payload});
            sent += 1;
        });

        const count = lines.filter((line) => line.trim()).length;
        const message = `Sent ${plural(count, "command")} to ` +
            `${plural(sent, "session")}.`;

        // The modal stays open: you usually want to see the result, and often
        // to send again.
        setBroadcastStatus(message, "ok");
        TW.toast(message);
    }

    $("bcSend").onclick = sendBroadcast;

    $("bcClearBtn").onclick = () => {
        $("bcCommands").value = "";
        updateBroadcastStatus();
        $("bcCommands").focus();
    };

    $("bcCommands").addEventListener("input", updateBroadcastStatus);

    $("bcCommands").addEventListener("keydown", (e) => {
        if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
            e.preventDefault();
            sendBroadcast();
        }
    });

    updateBroadcastStatus();
})();