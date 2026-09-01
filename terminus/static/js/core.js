/* Terminus — shared state, helpers, appearance, markdown, modal management.
   Creates the global TW namespace that every other script extends. Must load
   first; there is no module system.

   File path: terminus/static/js/core.js */

"use strict";

/* Vendored libraries must be present: everything below assumes them. A missing
   file is a deployment error, not a runtime condition to degrade around. */
(function checkVendors() {
    const missing = [
        ["xterm.min.js", typeof Terminal],
        ["xterm-addon-fit.min.js", typeof FitAddon],
        ["socket.io.min.js", typeof io],
    ].filter(([, type]) => type === "undefined").map(([file]) => file);

    if (!missing.length) return;

    document.addEventListener("DOMContentLoaded", () => {
        document.body.innerHTML =
            '<div style="font-family:system-ui;padding:40px;max-width:640px">' +
            "<h2>Terminus could not start</h2>" +
            "<p>Missing vendored libraries in " +
            "<code>terminus/static/js/vendor/</code>:</p><ul>" +
            missing.map((f) => `<li><code>${f}</code></li>`).join("") +
            "</ul><p>See the Setup section of the README.</p></div>";
    });
    throw new Error(`Terminus: missing vendor files — ${missing.join(", ")}`);
})();


const TW = {
    /* ---- storage keys ---- */
    RESTORE_KEY: "terminus_open_sessions",
    THEME_KEY: "terminus_theme",
    FONT_KEY: "terminus_font",
    FONT_SIZE_KEY: "terminus_font_size",
    PERF_KEY: "terminus_perf",
    SHELL_KEY: "terminus_shell",

    /* ---- appearance defaults ---- */
    DEFAULT_THEME: "light",
    DEFAULT_FONT: "Google Sans Code",
    FONT_FALLBACK: '"Google Sans Code", "JetBrains Mono", Consolas, monospace',
    DEFAULT_FONT_SIZE: 12.6,
    MIN_FONT_SIZE: 9,
    MAX_FONT_SIZE: 24,
    TOAST_MS: 2600,

    /* ---- live state ---- */
    socket: io("/terminus", {auth: {token: window.TERMINUS_TOKEN || ""}}),
    open: {},              // session_id -> {term, fit, wrap, name, status, …}
    activeId: null,
    aiBusySessions: new Set(),   // sessions the Assistant is running against

    /* ---- AI capability flags, refreshed from /api/ai/settings ---- */
    aiInstalled: false,    // the ai package is present server-side
    aiActive: false,       // enabled, disclaimed and configured
    aiTools: false,        // provider supports tool calling → Assistant usable
    aiProvider: "",

    /* Theme previews. `canvas` mirrors the --canvas gradient at swatch scale;
       `dots` are accent, ok, warn, err. */
    THEMES: [
        {
            id: "light", name: "Light",
            canvas: "radial-gradient(60px 40px at 10% 0%, #c6d8f3 0%, transparent 60%)," +
                "radial-gradient(60px 45px at 100% 15%, #bedcaf 0%, transparent 60%)," +
                "radial-gradient(55px 50px at 50% 110%, #d8a6d4 0%, transparent 65%)," +
                "linear-gradient(135deg, #eaf6ff 0%, #f3ecff 100%)",
            dots: ["#1565c0", "#27ae60", "#e67e22", "#c0392b"],
        },
        {
            id: "dark", name: "Dark",
            canvas: "radial-gradient(60px 40px at 10% 0%, #0c0c4c 0%, transparent 60%)," +
                "radial-gradient(60px 45px at 100% 15%, #1f501b 0%, transparent 60%)," +
                "radial-gradient(55px 50px at 50% 110%, #400e47 0%, transparent 65%)," +
                "linear-gradient(135deg, #14141c 0%, #4d4759 100%)",
            dots: ["#4fa3ff", "#27ae60", "#f39c12", "#e74c3c"],
        },
        {
            id: "solarized-light", name: "Solarized Light",
            canvas: "radial-gradient(60px 40px at 10% 0%, #bfe0d4 0%, transparent 60%)," +
                "radial-gradient(60px 45px at 100% 15%, #f2dfae 0%, transparent 60%)," +
                "radial-gradient(55px 50px at 50% 110%, #d9d2b4 0%, transparent 65%)," +
                "linear-gradient(135deg, #fdf6e3 0%, #f6efd8 100%)",
            dots: ["#1a7fc1", "#6b8f00", "#a37400", "#cb2b28"],
        },
        {
            id: "nord", name: "Nord",
            canvas: "radial-gradient(60px 40px at 10% 0%, #3f5474 0%, transparent 60%)," +
                "radial-gradient(60px 45px at 100% 15%, #2f5261 0%, transparent 60%)," +
                "radial-gradient(55px 50px at 50% 110%, #4a4f6b 0%, transparent 65%)," +
                "linear-gradient(135deg, #232831 0%, #3b4252 100%)",
            dots: ["#8fbcbb", "#b4d69a", "#f2d492", "#d97882"],
        },
        {
            id: "dracula", name: "Dracula",
            canvas: "radial-gradient(60px 40px at 10% 0%, #44306b 0%, transparent 60%)," +
                "radial-gradient(60px 45px at 100% 15%, #6b2f52 0%, transparent 60%)," +
                "radial-gradient(55px 50px at 50% 110%, #2b3a5c 0%, transparent 65%)," +
                "linear-gradient(135deg, #1e1f29 0%, #383a4c 100%)",
            dots: ["#bd93f9", "#50fa7b", "#ffb86c", "#ff5555"],
        },
        {
            id: "gruvbox", name: "Gruvbox",
            canvas: "radial-gradient(60px 40px at 10% 0%, #4a3a1e 0%, transparent 60%)," +
                "radial-gradient(60px 45px at 100% 15%, #3d2f22 0%, transparent 60%)," +
                "radial-gradient(55px 50px at 50% 110%, #503f2a 0%, transparent 65%)," +
                "linear-gradient(135deg, #1d2021 0%, #3c3836 100%)",
            dots: ["#fabd2f", "#b8bb26", "#fe8019", "#fb4934"],
        },
    ],

    /* Google Sans Code is bundled and always available. The rest load from
           Google Fonts and fall back to the bundled default when offline. */
    FONTS: [
        {id: "Google Sans Code", label: "Google Sans Code (bundled)"},
        {id: "JetBrains Mono", label: "JetBrains Mono (web)"},
        {id: "Fira Code", label: "Fira Code (web)"},
        {id: "IBM Plex Mono", label: "IBM Plex Mono (web)"},
        {id: "Source Code Pro", label: "Source Code Pro (web)"},
    ],
};


/* =========================================================================
   Small helpers
   ========================================================================= */
TW.uid = () => "s_" + Math.random().toString(36).slice(2, 10);
TW.$ = (id) => document.getElementById(id);
TW.fontStack = (id) => `"${id}", ${TW.FONT_FALLBACK}`;

/* Escape before interpolating into an innerHTML template.

   Device-controlled text — prompts, hostnames, log filenames, provider error
   messages — reaches innerHTML in several renderers. A hostname containing
   `"><img src=x onerror=...>` is a real threat here, not a theoretical one. */
TW.escapeHtml = (text) => String(text ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[ch]));

TW.esc = TW.escapeHtml;          // short alias for dense templates

TW.postJSON = function (url, body = null, method = "POST") {
    const opts = {method};
    if (body !== null) {
        opts.headers = {"Content-Type": "application/json"};
        opts.body = JSON.stringify(body);
    }
    return fetch(url, opts);
};

/* Pull the message out of a Flask abort() page. */
TW.errText = async function (res, fallback) {
    try {
        const body = await res.text();
        const match = body.match(/<p>(.*?)<\/p>/i);
        if (match) return match[1];
    } catch (_) { /* ignore */
    }
    return fallback;
};


/* =========================================================================
   Toast
   ========================================================================= */
let _toastTimer = null;

TW.toast = function (msg) {
    const el = TW.$("toast");
    if (!el) return;
    el.textContent = msg;
    el.classList.add("show");
    clearTimeout(_toastTimer);
    _toastTimer = setTimeout(() => el.classList.remove("show"), TW.TOAST_MS);
};


/* =========================================================================
   Clipboard
   ========================================================================= */
TW.copyToClipboard = async function (text) {
    try {
        if (navigator.clipboard?.writeText) {
            await navigator.clipboard.writeText(text);
            return;
        }
    } catch (_) { /* fall through to the legacy path */
    }

    try {
        const ta = document.createElement("textarea");
        ta.value = text;
        ta.style.position = "fixed";
        ta.style.opacity = "0";
        document.body.appendChild(ta);
        ta.focus();
        ta.select();
        document.execCommand("copy");
        ta.remove();
    } catch (_) { /* ignore */
    }
};

TW.readFromClipboard = async function () {
    try {
        if (navigator.clipboard?.readText) {
            return await navigator.clipboard.readText();
        }
    } catch (_) { /* ignore */
    }
    return "";
};


/* =========================================================================
   Modals — stack, focus trap, Escape, focus restore

   The stack matters: nested modals (Settings → AI disclaimer) must close one
   at a time. A naive "close every open overlay" handler would dismiss both.
   ========================================================================= */
const _modalStack = [];
let _preModalFocus = null;

const FOCUSABLE = [
    "a[href]",
    "button:not([disabled])",
    "input:not([disabled])",
    "select:not([disabled])",
    "textarea:not([disabled])",
    '[tabindex]:not([tabindex="-1"])',
].join(",");

function focusableWithin(root) {
    return [...root.querySelectorAll(FOCUSABLE)].filter(
        (el) => el.offsetParent !== null || el === document.activeElement);
}

TW.topModal = () => _modalStack[_modalStack.length - 1] || null;

TW.openModal = function (id) {
    const overlay = TW.$(id);
    if (!overlay || overlay.classList.contains("open")) return;

    if (!_modalStack.length) _preModalFocus = document.activeElement;
    overlay.classList.add("open");
    _modalStack.push(id);

    // Prefer the first real control over the close button, so Escape and Enter
    // both do something sensible immediately.
    const targets = focusableWithin(overlay);
    const first = targets.find((el) => !el.id?.startsWith("close")) || targets[0];
    if (first) setTimeout(() => first.focus(), 50);
};

TW.closeModal = function (id) {
    const overlay = TW.$(id);
    if (!overlay) return;
    overlay.classList.remove("open");

    const index = _modalStack.lastIndexOf(id);
    if (index !== -1) _modalStack.splice(index, 1);

    if (!_modalStack.length && _preModalFocus
        && document.contains(_preModalFocus)) {
        _preModalFocus.focus();
        _preModalFocus = null;
    }
};

document.addEventListener("keydown", (e) => {
    const top = TW.topModal();
    if (!top) return;

    if (e.key === "Escape") {
        // Escape belongs to the remote application while a terminal has focus
        // (vi, less, nested menus) — do not steal it.
        if (e.target.closest?.(".xterm")) return;
        e.preventDefault();
        TW.closeModal(top);
        return;
    }

    if (e.key !== "Tab") return;

    const overlay = TW.$(top);
    const targets = focusableWithin(overlay);
    if (!targets.length) return;

    const first = targets[0];
    const last = targets[targets.length - 1];

    if (!overlay.contains(document.activeElement)) {
        e.preventDefault();
        first.focus();
    } else if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
    }
});

/* Backdrop click, routed through closeModal so the stack and focus restore
   stay consistent. */
document.querySelectorAll(".modal-overlay").forEach((overlay) => {
    overlay.addEventListener("click", (e) => {
        if (e.target === overlay) TW.closeModal(overlay.id);
    });
});


/* =========================================================================
   Appearance — theme, font, performance mode
   ========================================================================= */
TW.currentFont = () => localStorage.getItem(TW.FONT_KEY) || TW.DEFAULT_FONT;
TW.currentFontStack = () => TW.fontStack(TW.currentFont());
TW.currentPerfMode = () => localStorage.getItem(TW.PERF_KEY) === "1";
TW.perfMode = () =>
    document.documentElement.getAttribute("data-perf") === "lite";

TW.currentFontSize = function () {
    const saved = parseFloat(localStorage.getItem(TW.FONT_SIZE_KEY));
    return Number.isFinite(saved) ? saved : TW.DEFAULT_FONT_SIZE;
};

TW.savePrefs = function (partial) {
    TW.postJSON("/api/prefs", partial).catch(() => { /* best effort */
    });
};

/* Re-fit and repaint every open terminal. xterm needs an explicit refresh
   after an option change; a hidden terminal cannot be fitted, hence safeFit. */
function refitAll() {
    Object.values(TW.open).forEach((o) => {
        try {
            o.fit.fit();
            o.term.refresh(0, o.term.rows - 1);
        } catch (_) { /* not laid out yet */
        }
    });
}

TW.applyTheme = function (id, {persist = true} = {}) {
    document.documentElement.setAttribute("data-theme", id);
    localStorage.setItem(TW.THEME_KEY, id);
    if (persist) TW.savePrefs({theme: id});
    TW.renderThemeGrid();

    // Desktop shell only: tint the native title bar to match.
    if (window.pywebview?.api?.set_titlebar_theme) {
        window.pywebview.api.set_titlebar_theme(id);
    }
};

TW.applyFont = function (id, {persist = true} = {}) {
    if (persist) {
        localStorage.setItem(TW.FONT_KEY, id);
        TW.savePrefs({font: id});
    }

    const stack = TW.fontStack(id);
    document.documentElement.style.setProperty("--font-mono", stack);

    // Wait for the face to load, or xterm measures the fallback and every
    // glyph lands a fraction of a pixel out.
    TW.ensureTermFont(id).then(() => {
        Object.values(TW.open).forEach((o) => {
            o.term.options.fontFamily = stack;
        });
        refitAll();
    });

    const preview = TW.$("fontPreview");
    if (preview) preview.style.fontFamily = stack;
};

TW.applyFontSize = function (size, {persist = true} = {}) {
    const px = Math.min(
        TW.MAX_FONT_SIZE,
        Math.max(TW.MIN_FONT_SIZE, parseFloat(size) || TW.DEFAULT_FONT_SIZE),
    );

    if (persist) {
        localStorage.setItem(TW.FONT_SIZE_KEY, String(px));
        TW.savePrefs({font_size: px});
    }

    document.documentElement.style.setProperty("--font-size-term", `${px}px`);
    Object.values(TW.open).forEach((o) => {
        o.term.options.fontSize = px;
    });
    refitAll();

    const input = TW.$("fontSizeInput");
    if (input && parseFloat(input.value) !== px) input.value = px;
    return px;
};

TW.applyPerfMode = function (on, {persist = true} = {}) {
    const lite = !!on;
    document.documentElement.setAttribute("data-perf", lite ? "lite" : "full");

    if (persist) {
        localStorage.setItem(TW.PERF_KEY, lite ? "1" : "0");
        TW.savePrefs({perf_mode: lite});
    }

    // An opaque background lets xterm take its fast rendering path.
    const theme = TW.buildTheme();
    Object.values(TW.open).forEach((o) => {
        o.term.options.theme = theme;
        o.term.options.allowTransparency = !lite;
    });
    refitAll();

    const input = TW.$("perfModeInput");
    if (input) input.checked = lite;
};

TW.renderThemeGrid = function () {
    const grid = TW.$("themeGrid");
    if (!grid) return;

    const active = localStorage.getItem(TW.THEME_KEY) || TW.DEFAULT_THEME;
    grid.innerHTML = "";

    TW.THEMES.forEach((theme) => {
        const isActive = theme.id === active;
        const card = document.createElement("div");
        card.className = "theme-card" + (isActive ? " active" : "");
        card.tabIndex = 0;
        card.setAttribute("role", "button");
        card.setAttribute("aria-label", `${theme.name} theme`);
        card.setAttribute("aria-pressed", isActive ? "true" : "false");

        const dots = theme.dots
            .map((c) => `<span class="sw-dot" style="background:${TW.esc(c)}"></span>`)
            .join("");

        card.innerHTML = `
            <div class="theme-swatch" style="background:${TW.esc(theme.canvas)}">
                <div class="sw-dots">${dots}</div>
            </div>
            <div class="theme-card-foot">
                <span>${TW.esc(theme.name)}</span>
                <span class="material-icons" aria-hidden="true">check_circle</span>
            </div>`;

        card.onclick = () => TW.applyTheme(theme.id);
        card.onkeydown = (e) => {
            if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                TW.applyTheme(theme.id);
            }
        };
        grid.appendChild(card);
    });
};

TW.renderAppearanceControls = function () {
    const select = TW.$("fontSelect");
    if (!select) return;

    const active = TW.currentFont();
    select.innerHTML = "";
    TW.FONTS.forEach((font) => {
        const option = document.createElement("option");
        option.value = font.id;
        option.textContent = font.label;
        option.selected = font.id === active;
        select.appendChild(option);
    });
    select.onchange = () => TW.applyFont(select.value);

    const size = TW.$("fontSizeInput");
    if (size) {
        size.value = TW.currentFontSize();
        size.onchange = () => TW.applyFontSize(size.value);
    }

    const perf = TW.$("perfModeInput");
    if (perf) {
        perf.checked = TW.perfMode();
        perf.onchange = () => TW.applyPerfMode(perf.checked);
    }

    const preview = TW.$("fontPreview");
    if (preview) {
        preview.textContent =
            "switch# show version\n" +
            "abcdefghijklmnop  0123456789\n" +
            "=> {} [] () <> || && == != -> =~";
        preview.style.fontFamily = TW.currentFontStack();
    }
};


/* =========================================================================
   xterm theming
   ========================================================================= */
TW.cssVar = (name, fallback) =>
    getComputedStyle(document.documentElement)
        .getPropertyValue(name).trim() || fallback;

TW.isDark = () => TW.cssVar("--terminal-variant", "dark") !== "light";

const ANSI_DARK = {
    black: "#2e3440", red: "#f14c4c", green: "#23d18b", yellow: "#f5f543",
    blue: "#3b8eea", magenta: "#d670d6", cyan: "#29b8db", white: "#e5e5e5",
    brightBlack: "#666666", brightRed: "#f14c4c", brightGreen: "#23d18b",
    brightYellow: "#f5f543", brightBlue: "#3b8eea", brightMagenta: "#d670d6",
    brightCyan: "#29b8db", brightWhite: "#ffffff",
};

const ANSI_LIGHT = {
    black: "#000000", red: "#cd3131", green: "#107c10", yellow: "#949800",
    blue: "#0451a5", magenta: "#bc05bc", cyan: "#0598bc", white: "#555555",
    brightBlack: "#666666", brightRed: "#cd3131", brightGreen: "#14ce14",
    brightYellow: "#b5ba00", brightBlue: "#0451a5", brightMagenta: "#bc05bc",
    brightCyan: "#0598bc", brightWhite: "#000000",
};

TW.buildTheme = function () {
    const dark = TW.isDark();
    const lite = TW.perfMode();
    return {
        background: lite
            ? TW.cssVar("--background-color-secondary",
                dark ? "#252526" : "#ffffff")
            : "rgba(0,0,0,0)",
        foreground: TW.cssVar("--text-color", dark ? "#ffffff" : "#000000"),
        cursor: TW.cssVar("--text-color", dark ? "#ffffff" : "#000000"),
        cursorAccent: TW.cssVar("--surface", dark ? "#252526" : "#ffffff"),
        selectionBackground: TW.cssVar("--active-background",
            dark ? "#2f3b4a" : "#e3ecf7"),
        ...(dark ? ANSI_DARK : ANSI_LIGHT),
    };
};

TW.termOptions = function () {
    return {
        cursorBlink: true,
        fontFamily: TW.currentFontStack(),
        fontSize: TW.currentFontSize(),
        lineHeight: 1.2,
        fontWeight: 400,
        allowProposedApi: true,
        allowTransparency: !TW.perfMode(),
        theme: TW.buildTheme(),
    };
};

TW.ensureTermFont = async function (fontId) {
    const family = fontId || TW.currentFont();
    if (!document.fonts || !family) return;
    try {
        await document.fonts.load(`400 ${TW.currentFontSize()}px "${family}"`);
        await document.fonts.ready;
    } catch (_) { /* the fallback stack will be used */
    }
};

/* Fit a terminal and tell the server its new geometry.

   Guards against fitting a hidden element: FitAddon divides by a measured cell
   height, which is 0 when the wrapper is not laid out. */
TW.safeFit = function (id) {
    const o = TW.open[id];
    if (!o || o.wrap.offsetParent === null || o.wrap.clientHeight === 0) return;
    try {
        o.fit.fit();
        TW.socket.emit("resize", {
            session_id: id, cols: o.term.cols, rows: o.term.rows,
        });
    } catch (_) { /* ignore */
    }
};

/* Repaint terminals when the theme or performance attribute flips. */
new MutationObserver(() => {
    const theme = TW.buildTheme();
    const lite = TW.perfMode();
    Object.values(TW.open).forEach((o) => {
        o.term.options.theme = theme;
        o.term.options.allowTransparency = !lite;
    });
    if (TW.activeId) TW.safeFit(TW.activeId);
}).observe(document.documentElement, {
    attributes: true,
    attributeFilter: ["data-theme", "data-perf"],
});


/* =========================================================================
   Markdown — marked parses, DOMPurify sanitises

   Both are required. marked passes raw HTML through by design (it dropped its
   own sanitize option in v5), and what we render is model output containing
   device data. Degrades to escaped plain text if either failed to load.
   ========================================================================= */
(function initMarkdown() {
    const available = typeof marked !== "undefined"
        && typeof DOMPurify !== "undefined";

    function caret() {
        const el = document.createElement("span");
        el.className = "cursor";
        return el;
    }

    if (!available) {
        console.error("marked/DOMPurify missing — check static/js/vendor/.");
        TW.markdownReady = false;
        TW.renderMarkdown = (src) =>
            `<pre class="md-fallback">${TW.escapeHtml(src)}</pre>`;
        TW.renderMarkdownInto = (el, src, streaming = false) => {
            el.innerHTML = TW.renderMarkdown(src);
            if (streaming) el.appendChild(caret());
        };
        return;
    }

    TW.markdownReady = true;

    marked.setOptions({
        gfm: true,          // tables, strikethrough, task lists, autolinks
        breaks: true,       // one newline is a line break — models assume this
        pedantic: false,
        headerIds: false,   // no ids: this is a transcript, not a document
        mangle: false,
    });

    // Links open in the OS browser, never inside the app frame.
    DOMPurify.addHook("afterSanitizeAttributes", (node) => {
        if (node.tagName === "A") {
            node.setAttribute("target", "_blank");
            node.setAttribute("rel", "noopener noreferrer nofollow");
        }
    });

    const PURIFY_CONFIG = {
        ALLOWED_TAGS: [
            "p", "br", "hr", "span", "div",
            "h1", "h2", "h3", "h4", "h5", "h6",
            "strong", "em", "del", "code", "pre", "blockquote",
            "ul", "ol", "li", "input",
            "table", "thead", "tbody", "tr", "th", "td",
            "a",
        ],
        ALLOWED_ATTR: ["href", "title", "class", "type", "checked", "disabled"],
        ALLOW_DATA_ATTR: false,
        FORBID_TAGS: ["style", "script", "iframe", "object", "embed", "form"],
        FORBID_ATTR: ["style", "srcset", "formaction"],
    };

    /* Render to an HTML string. Prefer renderMarkdownInto where possible. */
    TW.renderMarkdown = function (src) {
        try {
            return DOMPurify.sanitize(marked.parse(String(src ?? "")),
                PURIFY_CONFIG);
        } catch (err) {
            console.error("markdown render failed:", err);
            return `<pre class="md-fallback">${TW.escapeHtml(src)}</pre>`;
        }
    };

    /* Render into *el*, optionally appending the streaming caret.

       The caret is appended as a DOM node rather than markup so it cannot be
       swallowed by the sanitiser or absorbed into an unterminated code fence
       mid-stream. */
    TW.renderMarkdownInto = function (el, src, streaming = false) {
        el.innerHTML = TW.renderMarkdown(src);
        if (streaming) el.appendChild(caret());
    };
})();


/* =========================================================================
   AI capability gating
   ========================================================================= */
TW.refreshAIState = async function () {
    try {
        const res = await fetch("/api/ai/settings");
        if (res.ok) {
            const data = await res.json();
            TW.aiInstalled = data.available !== false;
            TW.aiActive = !!data.active;
            TW.aiTools = !!data.capabilities?.supports_tools;
            TW.aiProvider = data.provider || "";
        } else {
            TW.aiInstalled = false;
            TW.aiActive = false;
            TW.aiTools = false;
        }
    } catch (_) {
        TW.aiInstalled = false;
        TW.aiActive = false;
        TW.aiTools = false;
    }

    document.querySelectorAll("[data-ai-gated]").forEach((el) => {
        el.style.display = TW.aiActive ? "" : "none";
    });

    // Hide the whole settings page when the server has no ai package at all.
    const nav = document.querySelector(
        '.settings-nav-item[data-page="aiPage"]');
    if (nav) nav.style.display = TW.aiInstalled ? "" : "none";

    TW.onAIStateChange?.();
};


/* =========================================================================
   Socket diagnostics
   ========================================================================= */
let _connectFailures = 0;

TW.socket.on("connect", () => {
    _connectFailures = 0;
    TW.$("staleBanner")?.remove();
});

TW.socket.on("connect_error", (err) => {
    _connectFailures += 1;
    console.error(`socket connect_error (${_connectFailures}):`, err?.message);

    /* The launch token is regenerated on every start, so a page left open
       across a restart can never reconnect — retrying is pointless and the
       repeated toast is noise. Give up and say so once. */
    if (_connectFailures >= 3) {
        TW.socket.disconnect();
        showStaleBanner();
        return;
    }
    TW.toast("Reconnecting to Terminus…");
});

function showStaleBanner() {
    if (TW.$("staleBanner")) return;
    const banner = document.createElement("div");
    banner.id = "staleBanner";
    banner.innerHTML = `
        <span class="material-icons i-16" aria-hidden="true">sync_problem</span>
        <span>This page is out of date — Terminus restarted.</span>
        <button class="btn btn--primary" id="staleReload">Reload</button>`;
    document.body.appendChild(banner);
    TW.$("staleReload").onclick = () => location.reload();
}


/* =========================================================================
   Boot — apply persisted appearance

   localStorage first so there is no flash of the wrong theme, then reconcile
   against the server, which is the source of truth across browsers.
   ========================================================================= */
(async function boot() {
    const domTheme = document.documentElement.getAttribute("data-theme")
        || TW.DEFAULT_THEME;
    localStorage.setItem(TW.THEME_KEY, domTheme);

    TW.applyPerfMode(TW.currentPerfMode(), {persist: false});
    TW.applyFontSize(TW.currentFontSize(), {persist: false});
    TW.applyFont(TW.currentFont(), {persist: false});

    try {
        const res = await fetch("/api/prefs");
        if (res.ok) {
            const prefs = await res.json();

            if (typeof prefs.perf_mode === "boolean"
                && prefs.perf_mode !== TW.perfMode()) {
                localStorage.setItem(TW.PERF_KEY, prefs.perf_mode ? "1" : "0");
                TW.applyPerfMode(prefs.perf_mode, {persist: false});
            }
            if (prefs.theme && prefs.theme !== domTheme) {
                TW.applyTheme(prefs.theme, {persist: false});
            }
            if (prefs.font_size && prefs.font_size !== TW.currentFontSize()) {
                localStorage.setItem(TW.FONT_SIZE_KEY, String(prefs.font_size));
                TW.applyFontSize(prefs.font_size, {persist: false});
            }
            if (prefs.font && prefs.font !== TW.currentFont()) {
                TW.applyFont(prefs.font, {persist: false});
            } else {
                TW.renderThemeGrid();
            }
        }
    } catch (_) { /* offline, or no prefs saved yet */
    }

    TW.refreshAIState();

    if (!TW.markdownReady) {
        TW.toast("Markdown renderer failed to load — check static/js/vendor/.");
    }
})();