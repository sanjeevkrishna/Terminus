/* Terminus — terminal session lifecycle, sidebar rows, terminal header,
   broadcast, and socket events. Depends on core.js (TW namespace).
   File path: static/js/sessions.js */

"use strict";

(function () {
    const {$, socket, open} = TW;

    /* ===== Terminal header (status + active-session actions) ===== */
    function updateChip(id) {
        const o = TW.open[id];
        const title = $("chipTitle");
        const circle = $("chipCircle");
        const actions = $("termActions");

        if (!o) {
            title.textContent = "No active session";
            circle.className = "circle idle";
            actions.style.display = "none";
            return;
        }

        title.textContent = o.name;
        actions.style.display = "inline-flex";
        circle.className = `circle ${o.status}`;

        const dl = $("downloadBtn");
        if (dl && o.logname) dl.title = `Download ${o.logname}`;
    }

    TW.updateChip = updateChip;

    // Header action buttons act on the currently active session.
    $("downloadBtn").onclick = () => TW.activeId && downloadSessionLog(TW.activeId);
    $("closeBtn").onclick = () => TW.activeId && closeSession(TW.activeId);

    /* ===== Sidebar list (inline per-row actions) ===== */
    function renderOpen() {
        const list = $("openList");
        list.innerHTML = "";
        Object.entries(open).forEach(([id, o]) => {
            const status = o.status || "connecting";
            const el = document.createElement("div");
            el.className = "ts-item" + (id === TW.activeId ? " active" : "");
            el.innerHTML = `
                <span class="circle ${status}" title="${status}"></span>
                <span class="ts-item-name" title="${o.name}">${o.name}</span>
                <span class="ts-row-actions">
                    <button class="btn btn--sm" data-act="download" title="Download log">
                        <span class="material-icons i-16">download</span></button>
                    <button class="btn btn--sm btn--danger" data-act="close" title="Close session">
                        <span class="material-icons i-16">close</span></button>
                </span>`;
            el.querySelector('[data-act="download"]').onclick = (e) => {
                e.stopPropagation();
                downloadSessionLog(id);
            };
            el.querySelector('[data-act="close"]').onclick = (e) => {
                e.stopPropagation();
                closeSession(id);
            };
            el.onclick = () => activate(id);
            list.appendChild(el);
        });
    }

    TW.renderOpen = renderOpen;

    function setStatus(id, status) {
        const o = open[id];
        if (!o) return;
        o.status = status;
        if (id === TW.activeId) updateChip(id);
        renderOpen();
    }

    TW.setStatus = setStatus;

    /* ===== Session lifecycle ===== */
    function openSession(connector, hostname, activateNow = true) {
        const session_id = TW.uid();
        const wrap = document.createElement("div");
        wrap.dataset.id = session_id;
        $("terminals").appendChild(wrap);

        const term = new Terminal(TW.termOptions());
        const fit = new FitAddon.FitAddon();
        term.loadAddon(fit);
        term.open(wrap);
        term.onData((data) => socket.emit("input", {session_id, data}));

        term.onSelectionChange(() => {
            const sel = term.getSelection();
            if (sel) TW.copyToClipboard(sel);
        });

        wrap.addEventListener("contextmenu", async (e) => {
            e.preventDefault();
            const text = await TW.readFromClipboard();
            if (text) socket.emit("input", {session_id, data: text});
        });

        const ro = new ResizeObserver(() => TW.safeFit(session_id));
        ro.observe(wrap);

        open[session_id] = {
            term, fit, wrap, name: hostname, status: "connecting",
            connector, hostname, ro, logname: null,
        };

        TW.ensureTermFont().then(() => {
            const o = open[session_id];
            if (o) {
                o.term.options.fontFamily = TW.currentFontStack();
                TW.safeFit(session_id);
            }
        });

        socket.emit("join", {session_id});
        socket.emit("ssh_connect", {session_id, connector, hostname});
        renderOpen();
        saveOpenState();
        if (activateNow) activate(session_id);
        return session_id;
    }

    TW.openSession = openSession;

    function saveOpenState() {
        const list = Object.values(open).map(o => ({
            connector: o.connector, hostname: o.hostname,
        }));
        if (list.length) {
            sessionStorage.setItem(TW.RESTORE_KEY, JSON.stringify(list));
        } else {
            sessionStorage.removeItem(TW.RESTORE_KEY);
        }
    }

    function activate(id) {
        TW.activeId = id;
        $("placeholder").style.display = "none";
        document.querySelectorAll("#terminals > div[data-id]").forEach(w =>
            w.classList.toggle("shown", w.dataset.id === id));
        updateChip(id);
        const o = open[id];
        if (o) {
            requestAnimationFrame(() => {
                TW.safeFit(id);
                o.term.focus();
            });
        }
        renderOpen();
    }

    TW.activate = activate;

    function closeSession(id) {
        socket.emit("close_session", {session_id: id});
        const o = open[id];
        if (o) {
            o.ro?.disconnect();
            o.term.dispose();
            o.wrap.remove();
            delete open[id];
        }
        if (TW.activeId === id) {
            TW.activeId = null;
            const rest = Object.keys(open);
            if (rest.length) {
                activate(rest[0]);
            } else {
                $("placeholder").style.display = "flex";
                updateChip(null);
            }
        }
        renderOpen();
        saveOpenState();
    }

    TW.closeSession = closeSession;

    /* ===== Per-session log download ===== */
    async function downloadSessionLog(id) {
        try {
            const res = await fetch(`/download/${id}`);
            if (!res.ok) {
                const msg = await TW.errText(res, `Download failed: ${res.status}`);
                open[id]?.term.write(`\r\n*** ${msg} ***\r\n`);
                TW.toast(msg);
                return;
            }
            const cd = res.headers.get("Content-Disposition") || "";
            const match = cd.match(/filename\*?=(?:UTF-8'')?"?([^";]+)"?/i);
            const filename = (match && decodeURIComponent(match[1])) ||
                open[id]?.logname || `${id}.log`;
            TW.triggerDownload(await res.blob(), filename);
        } catch (err) {
            console.error("Log download failed:", err);
        }
    }

    TW.downloadSessionLog = downloadSessionLog;

    /* ===== Restore previous sessions on reconnect ===== */
    function restorePreviousSessions() {
        const raw = sessionStorage.getItem(TW.RESTORE_KEY);
        if (!raw) return;
        sessionStorage.removeItem(TW.RESTORE_KEY);

        let list = [];
        try { list = JSON.parse(raw); } catch (_) { /* ignore */ }
        if (!list.length) return;

        const summary = list.map(s => `• ${s.hostname} (${s.connector})`).join("\n");
        const ok = confirm(
            `Reconnect your ${list.length} previous session(s)?\n\n${summary}\n\n` +
            `Note: these are fresh connections — previous output is not restored.`
        );
        if (!ok) return;

        let firstId = null;
        list.forEach(({connector, hostname}) => {
            if (!connector || !hostname) return;
            const id = openSession(connector, hostname, false);
            if (!firstId) firstId = id;
        });
        if (firstId) activate(firstId);
    }

    /* ===== Broadcast ===== */
    const bInput = $("broadcastInput");

    function doBroadcast() {
        const cmd = bInput.value;
        if (!cmd.trim()) return;
        const line = cmd.endsWith("\n") ? cmd : cmd + "\n";

        let sent = 0;
        Object.entries(open).forEach(([id, o]) => {
            if (o.status === "active") {
                socket.emit("input", {session_id: id, data: line});
                sent++;
            }
        });

        bInput.value = "";
        bInput.focus();
        if (sent === 0) {
            bInput.placeholder = "No active sessions";
            setTimeout(() => { bInput.placeholder = "Send to all active…"; }, 1500);
        }
    }

    $("broadcastSend").onclick = doBroadcast;
    bInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
            e.preventDefault();
            doBroadcast();
        }
    });

    /* ===== Socket events ===== */
    socket.on("output", ({session_id, data}) => {
        open[session_id]?.term.write(data);
    });

    socket.on("session_ready", ({session_id, base_prompt, logname}) => {
        const o = open[session_id];
        if (o) {
            if (base_prompt) o.name = base_prompt;
            o.logname = logname;
            setStatus(session_id, "active");
            if (TW.activeId === session_id) updateChip(session_id);
            TW.safeFit(session_id);
        }
        TW.refreshLogsIfOpen?.();
    });

    socket.on("session_ended", ({session_id}) => {
        if (open[session_id]) setStatus(session_id, "idle");
        TW.refreshLogsIfOpen?.();
    });

    socket.on("connect", restorePreviousSessions);

    /* ===== Window events ===== */
    window.addEventListener("beforeunload", (e) => {
        if (Object.keys(open).length > 0) {
            e.preventDefault();
            e.returnValue = "";
        }
    });

    window.addEventListener("resize", () => {
        if (TW.activeId) TW.safeFit(TW.activeId);
    });
})();