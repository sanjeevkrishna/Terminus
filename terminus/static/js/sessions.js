/* Terminus — terminal session lifecycle, sidebar rows, terminal header,
   and session socket events. Depends on core.js (TW namespace).

   File path: terminus/static/js/sessions.js */

"use strict";

(function () {
    const {$, socket, open} = TW;

    /* Selection auto-copy: onSelectionChange fires continuously during a drag,
       so writing the clipboard on every event means dozens of async writes per
       gesture. Settle briefly instead. */
    const COPY_DEBOUNCE_MS = 180;

    /* Two frames, then a late catch-all: xterm needs the wrapper laid out
       before FitAddon can measure a cell, and a modal or font swap can shift
       geometry after the first paint. */
    const LATE_FIT_MS = 120;


    /* =====================================================================
       Terminal header — status dot, name, per-session actions
       ===================================================================== */
    function statusOf(id, session) {
        if (TW.aiBusySessions.has(id)) return "ai";
        return session.status || "connecting";
    }

    function updateChip(id) {
        const session = open[id];
        const title = $("chipTitle");
        const circle = $("chipCircle");
        const actions = $("termActions");

        if (!session) {
            title.textContent = "No active session";
            circle.className = "circle idle";
            circle.title = "";
            actions.style.display = "none";
            return;
        }

        title.textContent = session.name;
        title.title = session.name;
        actions.style.display = "inline-flex";

        const status = statusOf(id, session);
        circle.className = `circle ${status}`;
        circle.title = status;

        const logBtn = $("openLogBtn");
        if (logBtn) {
            logBtn.title = session.logname
                ? `Open ${session.logname}`
                : "Open log";
        }
    }

    TW.updateChip = updateChip;

    // Header actions always target the active session.
    $("askAiBtn").onclick = () =>
        TW.activeId && TW.askAboutSession?.(TW.activeId);
    $("openLogBtn").onclick = () =>
        TW.activeId && openSessionLog(TW.activeId);
    $("closeBtn").onclick = () =>
        TW.activeId && closeSession(TW.activeId);


    /* =====================================================================
       Sidebar list
       ===================================================================== */
    function renderOpen() {
        const list = $("openList");
        list.innerHTML = "";

        Object.entries(open).forEach(([id, session]) => {
            const status = statusOf(id, session);
            const safeName = TW.esc(session.name);

            const row = document.createElement("div");
            row.className = "ts-item" + (id === TW.activeId ? " active" : "");
            row.setAttribute("role", "listitem");
            row.tabIndex = 0;

            // session.name comes from the device prompt — escape it.
            row.innerHTML = `
                <span class="circle ${status}" title="${TW.esc(status)}"
                      aria-hidden="true"></span>
                <span class="ts-item-name" title="${safeName}">${safeName}</span>
                <span class="ts-row-actions">
                    <button class="btn btn--sm" data-act="open"
                            title="Open log" aria-label="Open log">
                        <span class="material-icons i-16"
                              aria-hidden="true">open_in_new</span>
                    </button>
                    <button class="btn btn--sm btn--danger" data-act="close"
                            title="Close session" aria-label="Close session">
                        <span class="material-icons i-16"
                              aria-hidden="true">close</span>
                    </button>
                </span>`;

            row.querySelector('[data-act="open"]').onclick = (e) => {
                e.stopPropagation();
                openSessionLog(id);
            };
            row.querySelector('[data-act="close"]').onclick = (e) => {
                e.stopPropagation();
                closeSession(id);
            };
            row.onclick = () => activate(id);
            row.onkeydown = (e) => {
                if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    activate(id);
                }
            };

            list.appendChild(row);
        });

        TW.onSessionsChanged?.();
    }

    TW.renderOpen = renderOpen;

    function setStatus(id, status) {
        const session = open[id];
        if (!session) return;
        session.status = status;
        if (id === TW.activeId) updateChip(id);
        renderOpen();
    }

    TW.setStatus = setStatus;

    /* Called by ai.chat.js when the Assistant starts or stops running commands,
       so the dot can show a distinct state. */
    TW.refreshAiMarks = function () {
        renderOpen();
        if (TW.activeId) updateChip(TW.activeId);
    };


    /* =====================================================================
       Terminal construction

       openSession and openShell shared ~40 identical lines; the terminal setup
       is factored out and each supplies only its own metadata and emit.
       ===================================================================== */
    function createTerminal(sessionId, meta) {
        const wrap = document.createElement("div");
        wrap.dataset.id = sessionId;
        $("terminals").appendChild(wrap);

        const term = new Terminal(TW.termOptions());
        const fit = new FitAddon.FitAddon();
        term.loadAddon(fit);
        term.open(wrap);

        term.onData((data) => socket.emit("input", {session_id: sessionId, data}));

        let copyTimer = null;
        term.onSelectionChange(() => {
            clearTimeout(copyTimer);
            copyTimer = setTimeout(() => {
                const selection = term.getSelection();
                if (selection) TW.copyToClipboard(selection);
            }, COPY_DEBOUNCE_MS);
        });

        // Right-click pastes, matching PuTTY and Windows Terminal.
        wrap.addEventListener("contextmenu", async (e) => {
            e.preventDefault();
            const text = await TW.readFromClipboard();
            if (text) socket.emit("input", {session_id: sessionId, data: text});
        });

        const observer = new ResizeObserver(() => TW.safeFit(sessionId));
        observer.observe(wrap);

        open[sessionId] = {
            term,
            fit,
            wrap,
            observer,
            copyTimer,
            status: "connecting",
            logname: null,
            ...meta,
        };

        // The font may still be loading; re-apply and refit once it lands, or
        // xterm measures the fallback and every glyph sits slightly off.
        TW.ensureTermFont().then(() => {
            const session = open[sessionId];
            if (!session) return;
            session.term.options.fontFamily = TW.currentFontStack();
            TW.safeFit(sessionId);
        });

        return sessionId;
    }

    function openSession(connector, hostname, activateNow = true) {
        const sessionId = TW.uid();
        createTerminal(sessionId, {
            name: hostname,
            connector,
            hostname,
            isShell: false,
        });

        socket.emit("join", {session_id: sessionId});
        socket.emit("ssh_connect", {session_id: sessionId, connector, hostname});

        renderOpen();
        saveOpenState();
        if (activateNow) activate(sessionId);
        return sessionId;
    }

    TW.openSession = openSession;

    function openShell(shellId = null, activateNow = true) {
        const sessionId = TW.uid();
        createTerminal(sessionId, {
            name: "Local Shell",
            connector: null,
            hostname: null,
            isShell: true,
        });

        socket.emit("join", {session_id: sessionId});
        socket.emit("open_shell", {session_id: sessionId, shell: shellId});

        renderOpen();
        saveOpenState();
        if (activateNow) activate(sessionId);
        return sessionId;
    }

    TW.openShell = openShell;


    /* =====================================================================
       Activation and teardown
       ===================================================================== */
    function activate(id) {
        TW.activeId = id;
        $("placeholder").style.display = "none";

        document.querySelectorAll("#terminals > div[data-id]").forEach((wrap) => {
            wrap.classList.toggle("shown", wrap.dataset.id === id);
        });

        updateChip(id);

        const session = open[id];
        if (session) {
            // Two frames so the wrapper is laid out before FitAddon measures.
            requestAnimationFrame(() => requestAnimationFrame(() => {
                TW.safeFit(id);
                session.term.focus();
            }));
            setTimeout(() => TW.safeFit(id), LATE_FIT_MS);
        }

        renderOpen();
    }

    TW.activate = activate;

    function closeSession(id) {
        socket.emit("close_session", {session_id: id});

        const session = open[id];
        if (session) {
            clearTimeout(session.copyTimer);
            session.observer?.disconnect();
            session.term.dispose();
            session.wrap.remove();
            delete open[id];
        }

        TW.aiBusySessions.delete(id);

        if (TW.activeId === id) {
            TW.activeId = null;
            const remaining = Object.keys(open);
            if (remaining.length) {
                activate(remaining[0]);
            } else {
                $("placeholder").style.display = "flex";
                updateChip(null);
            }
        }

        renderOpen();
        saveOpenState();
    }

    TW.closeSession = closeSession;


    /* =====================================================================
       Session log
       ===================================================================== */
    async function openSessionLog(id) {
        try {
            const res = await TW.postJSON(`/api/open/session/${id}`);
            if (!res.ok) {
                TW.toast(await TW.errText(res, `Open failed: ${res.status}`));
                return;
            }
            const data = await res.json().catch(() => ({}));
            if (!data.ok) TW.toast(data.message || "Could not open the log.");
        } catch (err) {
            console.error("openSessionLog failed:", err);
            TW.toast("Could not open the log.");
        }
    }

    TW.openSessionLog = openSessionLog;


    /* =====================================================================
       Restore after a page reload

       sessionStorage, so it survives F5 but not a new tab. Only fires on the
       first socket connect: on a transient drop the server has torn the
       sessions down but the restore list is still present, and reconnecting
       would append duplicates alongside the dead terminals.
       ===================================================================== */
    function saveOpenState() {
        const list = Object.values(open)
            .filter((session) => !session.isShell)   // shells are not restorable
            .map((session) => ({
                connector: session.connector,
                hostname: session.hostname,
            }));

        if (list.length) {
            sessionStorage.setItem(TW.RESTORE_KEY, JSON.stringify(list));
        } else {
            sessionStorage.removeItem(TW.RESTORE_KEY);
        }
    }

    function restorePreviousSessions() {
        const raw = sessionStorage.getItem(TW.RESTORE_KEY);
        if (!raw) return;
        sessionStorage.removeItem(TW.RESTORE_KEY);

        let list = [];
        try {
            list = JSON.parse(raw) || [];
        } catch (_) {
            return;
        }
        if (!list.length) return;

        const summary = list
            .map((s) => `• ${s.hostname} (${s.connector})`)
            .join("\n");
        const confirmed = confirm(
            `Reconnect your ${list.length} previous session(s)?\n\n${summary}` +
            `\n\nNote: these are fresh connections — previous output is not ` +
            `restored.`);
        if (!confirmed) return;

        let firstId = null;
        list.forEach(({connector, hostname}) => {
            if (!connector || !hostname) return;
            const id = openSession(connector, hostname, false);
            if (!firstId) firstId = id;
        });
        if (firstId) activate(firstId);
    }


    /* =====================================================================
       Socket events
       ===================================================================== */
    socket.on("output", ({session_id, data}) => {
        open[session_id]?.term.write(data);
    });

    socket.on("session_ready", ({session_id, base_prompt, logname}) => {
        const session = open[session_id];
        if (session) {
            // The device prompt is a better label than the hostname we dialled.
            if (base_prompt) session.name = base_prompt;
            session.logname = logname;
            setStatus(session_id, "active");
            TW.safeFit(session_id);
        }
        TW.refreshLogsIfOpen?.();
    });

    socket.on("session_ended", ({session_id}) => {
        if (open[session_id]) setStatus(session_id, "idle");
        TW.refreshLogsIfOpen?.();
    });

    /* The server drops keystrokes while the Assistant owns a channel, so the
       user gets an explanation rather than silence. */
    socket.on("input_blocked", ({session_id, message}) => {
        if (open[session_id]) TW.toast(message || "Input is blocked.");
    });

    let firstConnect = true;
    socket.on("connect", () => {
        if (!firstConnect) return;
        firstConnect = false;
        restorePreviousSessions();
    });


    /* =====================================================================
       Window events
       ===================================================================== */
    window.addEventListener("beforeunload", (e) => {
        // The desktop shell asks natively (confirm_close), so prompting here
        // too would ask twice.
        if (window.pywebview) return;
        if (Object.keys(open).length > 0) {
            e.preventDefault();
            e.returnValue = "";
        }
    });

    window.addEventListener("resize", () => {
        if (TW.activeId) TW.safeFit(TW.activeId);
    });
})();