/* Terminus — Assistant tab: streamed conversation, approval cards, execution
   progress.

   The Assistant never executes anything itself. It proposes commands, the
   server classifies them against terminus/ai/policy.py, and the user approves
   each batch. Every edit made in an approval card is re-validated server-side —
   this file is presentation, never a security boundary.

   Depends on core.js, sessions.js and tools.js (TW namespace).

   File path: terminus/static/js/ai.chat.js */

"use strict";

(function () {
    const {$, socket} = TW;

    const TAB_ASSISTANT = "tlAssistant";

    /* Follow the stream while the user is within this many pixels of the
       bottom. */
    const STICK_PX = 48;

    /* Only offer "Latest" once there is meaningfully more to see. */
    const JUMP_VISIBLE_PX = 120;

    const CHAT = {
        id: null,
        busy: false,
        streamEl: null,        // live assistant bubble being appended to
        streamText: "",
        raf: null,             // pending render frame during streaming
        stick: true,           // follow output? false once the user scrolls up
        cards: new Map(),      // plan_id -> {approvalEl, execEl, rows: Map}
    };

    /* The model only ever sees aliases (S1, S2). chat_scope tells us which
       session each one maps to, so the sidebar can show which devices are
       being worked on. */
    let scopeMap = {};

    const RISK_LABEL = {
        read_only: "read only",
        mutating: "changes config",
        destructive: "destructive",
        forbidden: "refused",
        unknown: "not permitted",
    };

    const STATUS_ICON = {
        busy: "progress_activity",
        ok: "check_circle",
        idle: "check_circle",
        error: "error",
    };

    const EXEC_ICON = {
        ok: "✔", error: "✖", timeout: "⏱", busy: "⏸", blocked: "⛔",
        wrong_mode: "⚠", session_gone: "✖", cancelled: "•", skipped: "•",
        locked: "⏸",
    };

    function plural(count, noun) {
        return `${count} ${noun}${count === 1 ? "" : "s"}`;
    }


    /* =====================================================================
       Status bar — always visible, and diagnostic on sight

       Ready → Sending… (client only) → Contacting the model… (server got it)
       → Thinking… → Waiting for your approval → Running… → Done
       ===================================================================== */
    function setStatus(text, kind = "busy") {
        const bar = $("chatStatus");
        bar.className = `tl-status tl-status--${kind}`;
        $("chatStatusText").textContent = text;

        const icon = bar.querySelector(".tl-status-icon");
        if (icon) {
            icon.textContent = STATUS_ICON[kind] || "info";
            icon.classList.toggle("spin", kind === "busy");
        }
    }

    function clearStatus() {
        setStatus("Ready", "idle");
    }

    function setBusy(on) {
        CHAT.busy = on;
        $("chatSendBtn").disabled = on;
        $("chatInput").disabled = on;
        $("chatStopBtn").style.display = on ? "inline-flex" : "none";
    }

    /* Everything a finished, failed or cancelled turn must undo. */
    function finishTurn() {
        setBusy(false);
        clearAiMarks();
        stopExecSpinners();
    }


    /* =====================================================================
       Conversation lifecycle
       ===================================================================== */
    function ensureChat() {
        if (CHAT.id) return CHAT.id;
        CHAT.id = "c_" + Math.random().toString(36).slice(2, 12);
        socket.emit("chat_start", {
            chat_id: CHAT.id,
            auto_approve: $("chatAutoApprove").checked,
        });
        return CHAT.id;
    }

    function resetChat() {
        if (CHAT.id) socket.emit("chat_reset", {chat_id: CHAT.id});

        cancelPendingRender();
        CHAT.cards.clear();
        CHAT.streamEl = null;
        CHAT.streamText = "";
        CHAT.stick = true;
        scopeMap = {};

        clearAiMarks();
        $("chatLog").innerHTML = "";
        $("chatLog").appendChild(emptyState());
        setBusy(false);
        clearStatus();
        updateJumpButton();
    }

    /* Mirrors the markup in terminus.html — the template provides the initial
       state, this rebuilds it after a reset. */
    function emptyState() {
        const el = document.createElement("div");
        el.className = "chat-empty";
        el.id = "chatEmpty";
        el.innerHTML = `
            <span class="material-icons" aria-hidden="true">auto_awesome</span>
            <div class="chat-empty-title">Ask about the selected sessions</div>
            <div class="chat-empty-hint">
                The Assistant reads each session's recent output and can propose
                read-only commands. Nothing runs without your approval.
            </div>
            <div class="chat-examples">
                <button class="chat-chip">Give me a discovery report for the selected devices</button>
                <button class="chat-chip">Compare these devices and highlight differences</button>
                <button class="chat-chip">Are there any interface errors I should worry about?</button>
            </div>`;
        wireChips(el);
        return el;
    }

    function wireChips(root) {
        root.querySelectorAll(".chat-chip").forEach((chip) => {
            chip.onclick = () => {
                $("chatInput").value = chip.textContent.trim();
                send();
            };
        });
    }

    wireChips(document);


/* =====================================================================
       Scroll engine

       Sticky-follow is driven by scroll *events*, not by measuring after a
       mutation: a delta larger than the threshold would already have pushed us
       out of range by the time we looked.

       But a programmatic pin also fires a scroll event, and if content grew
       between the pin and that event the measured distance looks exactly like
       a manual scroll-up — which silently switched following off. So pins are
       marked, and detaching is driven by real input gestures.
       ===================================================================== */
    const PIN_GRACE_MS = 150;
    let lastPinAt = 0;

    function log() {
        return $("chatLog");
    }

    function distanceFromBottom() {
        const el = log();
        return el.scrollHeight - el.scrollTop - el.clientHeight;
    }

    function pin() {
        const el = log();
        el.scrollTop = el.scrollHeight;
        lastPinAt = performance.now();
    }

    function scroll(force = false) {
        if (force) CHAT.stick = true;
        if (CHAT.stick) pin();
        updateJumpButton();
    }

    /* Content height settles after insertion — markdown tables lay out, code
       blocks wrap, webfonts swap. Pin now and again next frame; growthObserver
       covers anything later still. */
    function pinSettled(force = false) {
        scroll(force);
        requestAnimationFrame(() => scroll(force));
    }

    function updateJumpButton() {
        const button = $("chatJumpBtn");
        if (!button) return;
        button.classList.toggle("show",
            !CHAT.stick && distanceFromBottom() > JUMP_VISIBLE_PX);
    }

    $("chatLog").addEventListener("scroll", () => {
        // Ignore the echo of our own pin; only re-attach on reaching bottom.
        if (performance.now() - lastPinAt < PIN_GRACE_MS) {
            updateJumpButton();
            return;
        }
        CHAT.stick = distanceFromBottom() <= STICK_PX;
        updateJumpButton();
    }, {passive: true});

    /* Detaching on an explicit gesture bypasses the grace window, so scrolling
       up mid-stream still works even while pins are firing every frame. */
    function detachOnUpwardGesture(event) {
        if (event.deltaY < 0) {
            CHAT.stick = false;
            updateJumpButton();
        }
    }

    $("chatLog").addEventListener("wheel", detachOnUpwardGesture, {passive: true});

    $("chatLog").addEventListener("keydown", (e) => {
        if (["ArrowUp", "PageUp", "Home"].includes(e.key)) {
            CHAT.stick = false;
            updateJumpButton();
        }
    });

    /* Content changes height after insertion; re-pin while following. */
    const growthObserver = new ResizeObserver(() => {
        if (CHAT.stick) pin();
    });

    function follow(el) {
        growthObserver.observe(el);
        return el;
    }

    $("chatJumpBtn").onclick = () => {
        CHAT.stick = true;
        pin();
        updateJumpButton();
    };

    /* =====================================================================
       Log elements
       ===================================================================== */
    function dropEmptyState() {
        $("chatEmpty")?.remove();
    }

    function appendToLog(el, force) {
        dropEmptyState();
        log().appendChild(follow(el));
        scroll(force);
        return el;
    }

    function bubble(role, html, force = false) {
        const el = document.createElement("div");
        el.className = `chat-msg chat-${role}`;
        el.innerHTML = html;
        return appendToLog(el, force);
    }

    function card(className, force = false) {
        const el = document.createElement("div");
        el.className = className;
        return appendToLog(el, force);
    }

    function cancelPendingRender() {
        if (CHAT.raf === null) return;
        cancelAnimationFrame(CHAT.raf);
        CHAT.raf = null;
    }

    /* Close the open streaming bubble: drop the caret and stop re-rendering. */
    function settleStream() {
        if (!CHAT.streamEl) return;
        cancelPendingRender();
        TW.renderMarkdownInto(CHAT.streamEl, CHAT.streamText, false);
        CHAT.streamEl = null;
    }


    /* =====================================================================
       Sidebar activity markers
       ===================================================================== */
    function markAi(sessionIds) {
        sessionIds.forEach((id) => TW.aiBusySessions.add(id));
        TW.refreshAiMarks?.();
    }

    function clearAiMarks() {
        if (!TW.aiBusySessions.size) return;
        TW.aiBusySessions.clear();
        TW.refreshAiMarks?.();
    }


    /* =====================================================================
       Sending
       ===================================================================== */
    function send() {
        const text = $("chatInput").value.trim();
        if (!text) return;

        if (CHAT.busy) {
            TW.toast("Wait for the current answer to finish.");
            return;
        }

        const sessionIds = TW.tools.selectedIds();
        if (!sessionIds.length) {
            TW.toast("Select at least one session first.");
            return;
        }
        if (!TW.aiTools) {
            TW.toast("The Assistant needs a tool-calling provider.");
            return;
        }

        const chatId = ensureChat();

        // An unanswered card from an earlier question can no longer be
        // approved — the server has moved on.
        settleStaleCards("Superseded by a newer question");

        bubble("user", TW.renderMarkdown(text), true);
        $("chatInput").value = "";
        CHAT.streamEl = null;
        CHAT.streamText = "";

        setBusy(true);
        setStatus("Sending…", "busy");
        socket.emit("chat_send", {
            chat_id: chatId, text, session_ids: sessionIds,
        });
    }

    $("chatSendBtn").onclick = send;

    $("chatInput").addEventListener("keydown", (e) => {
        if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
            e.preventDefault();
            send();
        }
    });

    $("chatStopBtn").onclick = () => {
        if (!CHAT.id) return;
        socket.emit("chat_cancel", {chat_id: CHAT.id});
        setStatus("Stopping…", "busy");
    };

    $("chatResetBtn").onclick = () => {
        if (CHAT.busy) {
            if (!confirm("Cancel the current answer and clear the chat?")) return;
            if (CHAT.id) socket.emit("chat_cancel", {chat_id: CHAT.id});
        }
        resetChat();
    };

    $("chatAutoApprove").onchange = () => {
        if (!CHAT.id) return;
        socket.emit("chat_auto_approve", {
            chat_id: CHAT.id, enabled: $("chatAutoApprove").checked,
        });
    };


    /* =====================================================================
       Approval card
       ===================================================================== */
    function riskBadge(risk) {
        const label = RISK_LABEL[risk] || risk;
        return `<span class="risk-badge risk-${TW.esc(risk)}">` +
            `${TW.esc(label)}</span>`;
    }

    function blockedRowsHtml(blocked) {
        return (blocked || []).map((item) => `
            <div class="ap-blocked-row">
                <span class="ap-alias">${TW.esc(item.alias)}</span>
                <code class="ap-strike">${TW.esc(item.command)}</code>
                <span class="ap-blocked-why">${TW.esc(item.reason)}</span>
            </div>`).join("");
    }

    function cardEntry(planId) {
        let entry = CHAT.cards.get(planId);
        if (!entry) {
            entry = {approvalEl: null, execEl: null, rows: new Map()};
            CHAT.cards.set(planId, entry);
        }
        return entry;
    }

    function settleCard(el, label) {
        el.classList.add("ap-settled");
        el.querySelectorAll("input, button").forEach((node) => {
            node.disabled = true;
        });
        const note = el.querySelector('[data-role="note"]');
        if (note) note.textContent = label;
    }

    /* Disable any card that was never answered, so an abandoned card cannot
       emit an approval for a plan_id the server has forgotten. */
    function settleStaleCards(label) {
        CHAT.cards.forEach((entry) => {
            const el = entry.approvalEl;
            if (el && !el.classList.contains("ap-settled")) settleCard(el, label);
        });
    }

    function renderApproval(data) {
        const el = card("ap-card", true);
        const items = data.items || [];
        const blocked = data.blocked || [];
        const devices = new Set(items.map((item) => item.alias)).size;

        const rows = items.map((item, index) => `
            <div class="ap-row" data-index="${index}">
                <input type="checkbox" checked title="Include this command"
                       aria-label="Include ${TW.esc(item.command)}"/>
                <span class="ap-alias"
                      title="${TW.esc(item.hostname)}">${TW.esc(item.alias)}</span>
                <span class="ap-host">${TW.esc(item.hostname)}</span>
                <input class="ap-cmd mono" value="${TW.esc(item.command)}"
                       spellcheck="false" aria-label="Command"/>
                ${riskBadge(item.risk)}
            </div>`).join("");

        const blockedSection = blocked.length ? `
            <div class="ap-blocked">
                <div class="ap-blocked-head">
                    <span class="material-icons i-16" aria-hidden="true">block</span>
                    Refused by Terminus (${blocked.length})
                </div>
                ${blockedRowsHtml(blocked)}
            </div>` : "";

        // Only reachable once the server's risk ceiling is raised past
        // read_only; the machinery is in place for that.
        const confirmSection = data.needs_confirmation ? `
            <div class="ap-confirm">
                <label>
                    This batch changes device state. Type
                    <code>${TW.esc(data.confirmation_phrase)}</code> to confirm:
                </label>
                <input class="ap-confirm-input mono" spellcheck="false"
                       aria-label="Confirmation phrase"
                       placeholder="${TW.esc(data.confirmation_phrase)}"/>
            </div>` : "";

        el.innerHTML = `
            <div class="ap-head">
                <span class="material-icons i-16" aria-hidden="true">policy</span>
                <span class="ap-title">Approval required</span>
                <span class="ap-count">${plural(items.length, "command")}
                    · ${plural(devices, "device")}</span>
            </div>
            ${data.reason ? `<div class="ap-reason">${TW.esc(data.reason)}</div>` : ""}
            <div class="ap-rows">${rows}</div>
            ${blockedSection}
            ${confirmSection}
            <div class="ap-actions">
                <span class="ap-note" data-role="note"></span>
                <button class="btn btn--ghost" data-act="deny">Deny</button>
                <button class="btn btn--primary" data-act="run">
                    <span class="material-icons i-16" aria-hidden="true">play_arrow</span>Run selected
                </button>
            </div>`;

        const runButton = el.querySelector('[data-act="run"]');
        const denyButton = el.querySelector('[data-act="deny"]');
        const note = el.querySelector('[data-role="note"]');
        const confirmInput = el.querySelector(".ap-confirm-input");

        /* Ticked rows, with any edits the user made. The server re-validates
           every command, so an edit cannot escalate privilege. */
        function chosen() {
            return [...el.querySelectorAll(".ap-row")]
                .filter((row) =>
                    row.querySelector('input[type="checkbox"]').checked)
                .map((row) => ({
                    alias: items[Number(row.dataset.index)].alias,
                    command: row.querySelector(".ap-cmd").value.trim(),
                }))
                .filter((entry) => entry.command);
        }

        function refresh() {
            const count = chosen().length;
            let allowed = count > 0;
            if (confirmInput) {
                allowed = allowed
                    && confirmInput.value.trim() === data.confirmation_phrase;
            }
            runButton.disabled = !allowed;
            note.textContent = count === 0
                ? "Nothing selected"
                : `${plural(count, "command")} will run`;
        }

        el.querySelectorAll(".ap-row input").forEach((input) => {
            input.addEventListener("input", refresh);
            input.addEventListener("change", refresh);
        });
        confirmInput?.addEventListener("input", refresh);
        refresh();

        runButton.onclick = () => {
            const approved = chosen();
            markAi(approved
                .map((entry) => scopeMap[entry.alias])
                .filter(Boolean));
            socket.emit("chat_approve", {
                chat_id: CHAT.id, plan_id: data.plan_id, items: approved,
            });
            settleCard(el, `Approved — ${plural(approved.length, "command")}`);
            setStatus("Running commands…", "busy");
        };

        denyButton.onclick = () => {
            socket.emit("chat_deny", {
                chat_id: CHAT.id,
                plan_id: data.plan_id,
                reason: "The user declined to run these commands.",
            });
            settleCard(el, "Denied");
            setStatus("Thinking…", "busy");
        };

        cardEntry(data.plan_id).approvalEl = el;
        pinSettled(true);
    }

    /* Two cases: the model proposed something policy refused outright, or the
       user's edits were refused after approval. */
    function renderRefused(data) {
        const el = card("ap-card ap-card--rejected", true);
        const title = data.post_edit
            ? "Some edited commands were refused"
            : "Terminus refused this request";

        el.innerHTML = `
            <div class="ap-head">
                <span class="material-icons i-16" aria-hidden="true">block</span>
                <span class="ap-title">${TW.esc(title)}</span>
            </div>
            ${data.reason ? `<div class="ap-reason">${TW.esc(data.reason)}</div>` : ""}
            <div class="ap-blocked">${blockedRowsHtml(data.blocked)}</div>`;
        pinSettled(true);
    }


    /* =====================================================================
       Execution progress
       ===================================================================== */
    function execCard(planId) {
        const entry = cardEntry(planId);
        if (!entry.execEl) {
            entry.execEl = card("ex-card");
            entry.execEl.innerHTML = `
                <div class="ex-head">
                    <span class="material-icons i-16 spin"
                          aria-hidden="true">progress_activity</span>
                    <span>Running commands</span>
                </div>
                <div class="ex-rows"></div>`;
            pinSettled();
        }
        return entry;
    }

    function execRow(planId, alias, command) {
        const entry = execCard(planId);
        const key = `${alias}\u0000${command}`;

        let row = entry.rows.get(key);
        if (!row) {
            row = document.createElement("div");
            row.className = "ex-row";
            row.innerHTML = `
                <span class="ex-status" data-role="status">…</span>
                <span class="ex-alias">${TW.esc(alias)}</span>
                <code class="ex-cmd">${TW.esc(command)}</code>
                <span class="ex-meta" data-role="meta"></span>
                <button class="btn btn--sm" data-role="toggle"
                        style="visibility:hidden;" title="Show output"
                        aria-label="Show output" aria-expanded="false">
                    <span class="material-icons i-16"
                          aria-hidden="true">expand_more</span>
                </button>`;
            entry.execEl.querySelector(".ex-rows").appendChild(row);
            entry.rows.set(key, row);
        }
        return row;
    }

    function onExec(data) {
        const row = execRow(data.plan_id, data.alias, data.command);
        const status = row.querySelector('[data-role="status"]');
        const meta = row.querySelector('[data-role="meta"]');

        status.textContent = EXEC_ICON[data.status] || "•";
        status.className = `ex-status ex-${TW.esc(data.status)}`;
        status.title = data.status + (data.detail ? ` — ${data.detail}` : "");

        const bytes = data.bytes ? `${data.bytes.toLocaleString()} B` : "";
        meta.textContent = [bytes, `${data.elapsed}s`]
            .filter(Boolean).join(" · ");

        if (data.detail) {
            meta.title = data.detail;
            row.classList.add("ex-row--detail");
        }
        scroll();
    }

    /* Raw captured output, collapsed by default. Users want to verify what the
       model actually saw. */
    function onExecOutput(data) {
        const row = execRow(data.plan_id, data.alias, data.command);
        const toggle = row.querySelector('[data-role="toggle"]');

        let pre = row.nextElementSibling;
        if (!pre || !pre.classList.contains("ex-output")) {
            pre = document.createElement("pre");
            pre.className = "ex-output";
            row.after(pre);
        }

        // textContent, not innerHTML: this is unsanitised device output.
        pre.textContent = data.output +
            (data.truncated ? "\n\n[output truncated]" : "");

        toggle.style.visibility = "visible";
        scroll();
        toggle.onclick = () => {
            const shown = pre.classList.toggle("open");
            toggle.setAttribute("aria-expanded", shown ? "true" : "false");
            toggle.querySelector(".material-icons").textContent =
                shown ? "expand_less" : "expand_more";
            if (shown) scroll();
        };
    }

    function stopExecSpinners() {
        CHAT.cards.forEach((entry) => {
            entry.execEl?.querySelector(".ex-head .spin")
                ?.classList.remove("spin");
        });
    }


    /* =====================================================================
       Socket events
       ===================================================================== */
    function mine(data) {
        return data && data.chat_id === CHAT.id;
    }

    /* Proves the server received the message: if the status never advances past
       "Sending…", the event did not arrive. */
    socket.on("chat_ack", (data) => {
        if (mine(data)) setStatus("Contacting the model…", "busy");
    });

    socket.on("chat_state", (data) => {
        if (!mine(data)) return;

        switch (data.state) {
        case "thinking":
            setStatus(data.round > 1
                ? `Thinking… (round ${data.round} of ${data.max_rounds})`
                : "Thinking…", "busy");
            break;
        case "awaiting_approval":
            setStatus("Waiting for your approval", "busy");
            break;
        case "executing":
            setStatus("Running commands on your devices…", "busy");
            break;
        case "cancelled":
            settleStream();
            settleStaleCards("Cancelled");
            setStatus("Stopped.", "error");
            finishTurn();
            break;
        default:
            break;
        }
    });

    socket.on("chat_scope", (data) => {
        if (!mine(data)) return;
        scopeMap = {};
        (data.sessions || []).forEach((entry) => {
            if (entry.alias) scopeMap[entry.alias] = entry.session_id || null;
        });
    });

    socket.on("chat_delta", (data) => {
        if (!mine(data)) return;

        if (!CHAT.streamEl) {
            CHAT.streamEl = bubble("assistant", "");
            CHAT.streamText = "";
        }
        CHAT.streamText += data.text;

        // Re-parsing the whole message per delta is O(n²) over its length, and
        // deltas arrive faster than the browser paints — coalesce to one render
        // per frame.
        if (CHAT.raf !== null) return;
        CHAT.raf = requestAnimationFrame(() => {
            CHAT.raf = null;
            if (!CHAT.streamEl) return;
            TW.renderMarkdownInto(CHAT.streamEl, CHAT.streamText, true);
        });
    });

    socket.on("chat_message", (data) => {
        if (!mine(data)) return;

        if (CHAT.streamEl) {
            cancelPendingRender();
            TW.renderMarkdownInto(CHAT.streamEl, data.text, false);
            CHAT.streamEl = null;
            CHAT.streamText = "";
        } else if (data.text?.trim()) {
            bubble("assistant", TW.renderMarkdown(data.text));
        }
    });

    socket.on("chat_plan", (data) => {
        if (!mine(data)) return;
        settleStream();          // close the bubble before the card
        if (data.auto_rejected || data.post_edit) renderRefused(data);
        else renderApproval(data);
    });

    socket.on("chat_exec", (data) => {
        if (mine(data)) onExec(data);
    });

    socket.on("chat_exec_output", (data) => {
        if (mine(data)) onExecOutput(data);
    });

    socket.on("chat_error", (data) => {
        if (!mine(data)) return;
        // Keep any partial answer visible above the error.
        settleStream();
        setStatus(data.message || "The Assistant failed.", "error");
        finishTurn();
    });

    socket.on("chat_done", (data) => {
        if (!mine(data)) return;
        settleStream();
        setStatus("Done", "ok");
        finishTurn();
    });


    /* =====================================================================
       Provider capability gate
       ===================================================================== */
    function applyGate() {
        const gate = $("chatGate");
        if (!gate) return;
        const foot = document.querySelector(`#${TAB_ASSISTANT} .tl-foot`);

        if (!TW.aiActive || TW.aiTools) {
            gate.style.display = "none";
            if (foot) foot.style.display = "";
            return;
        }

        gate.style.display = "";
        gate.innerHTML = `
            <span class="material-icons i-16" aria-hidden="true">info</span>
            <span>The Assistant needs tool calling, which is not enabled for the
                current provider${TW.aiProvider
                    ? ` (<strong>${TW.esc(TW.aiProvider)}</strong>)` : ""}.
                For Ollama, tick <strong>Enable the interactive Assistant</strong>
                and use a large tool-calling model. See
                <strong>Settings → AI</strong>.</span>`;
        if (foot) foot.style.display = "none";
        setStatus("Assistant unavailable", "error");
    }

    TW.onAIStateChange = applyGate;


    /* =====================================================================
       External entry points
       ===================================================================== */
    /* "Ask about this session" in the terminal header: open the Tools panel on
       the Assistant tab with just that session selected. */
    TW.askAboutSession = function (sessionId) {
        if (!sessionId) return;
        TW.tools.openModal(TAB_ASSISTANT, sessionId);
    };

    /* Called by tools.js on every close path, including Escape and backdrop.

       Cancelling matters: a turn blocked awaiting approval would otherwise hold
       the server in _await_decision for its full timeout, and every later
       question would be refused as "already in progress". */
    TW.onToolsClose = function () {
        $("chatAutoApprove").checked = false;
        if (!CHAT.id) return;

        socket.emit("chat_auto_approve", {chat_id: CHAT.id, enabled: false});

        if (CHAT.busy) {
            socket.emit("chat_cancel", {chat_id: CHAT.id});
            settleStream();
            settleStaleCards("Abandoned — the Tools panel was closed");
            finishTurn();
            clearStatus();
        }
    };

    clearStatus();
})();