/* Terminus — shared state, helpers, appearance (theme + font), xterm theming.
   Exposes the global TW namespace consumed by sessions.js and settings.js.
   File path: static/js/core.js */

"use strict";

const TW = {
    // constants
    RESTORE_KEY: "terminus_open_sessions",
    THEME_KEY: "terminus_theme",
    FONT_KEY: "terminus_font",
    DEFAULT_THEME: "dark",
    DEFAULT_FONT: "Google Sans Code",
    FONT_FALLBACK: '"Google Sans Code", "JetBrains Mono", Consolas, monospace',

    // live state
    socket: io("/terminus"),
    open: {},            // session_id -> session object
    activeId: null,

    // catalogs
    THEMES: [
        {id: "light", name: "Light"},
        {id: "dark", name: "Dark"},
    ],
    FONTS: [
        {id: "Google Sans Code", label: "Google Sans Code (default)"},
        {id: "JetBrains Mono", label: "JetBrains Mono"},
        {id: "Fira Code", label: "Fira Code"},
        {id: "IBM Plex Mono", label: "IBM Plex Mono"},
        {id: "Source Code Pro", label: "Source Code Pro"},
    ],
};

/* ===== Small helpers ===== */
TW.uid = () => "s_" + Math.random().toString(36).slice(2, 10);
TW.$ = (id) => document.getElementById(id);
TW.fontStack = (id) => `"${id}", ${TW.FONT_FALLBACK}`;

let _toastTimer = null;
TW.toast = function (msg) {
    const t = TW.$("toast");
    t.textContent = msg;
    t.classList.add("show");
    clearTimeout(_toastTimer);
    _toastTimer = setTimeout(() => t.classList.remove("show"), 2600);
};

TW.errText = async function (res, fallback) {
    try {
        const body = await res.text();
        const m = body.match(/<p>(.*?)<\/p>/i);
        if (m) return m[1];
    } catch (_) { /* ignore */ }
    return fallback;
};

TW.triggerDownload = function (blob, filename) {
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
};

TW.openModal = (id) => TW.$(id).classList.add("open");
TW.closeModal = (id) => TW.$(id).classList.remove("open");

/* ===== Clipboard ===== */
TW.copyToClipboard = async function (text) {
    try {
        if (navigator.clipboard?.writeText) {
            await navigator.clipboard.writeText(text);
            return;
        }
    } catch (_) { /* fall through */ }
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
    } catch (_) { /* ignore */ }
};

TW.readFromClipboard = async function () {
    try {
        if (navigator.clipboard?.readText) {
            return await navigator.clipboard.readText();
        }
    } catch (_) { /* ignore */ }
    return "";
};

/* ===== Appearance: theme + font ===== */
TW.currentFont = () => localStorage.getItem(TW.FONT_KEY) || TW.DEFAULT_FONT;
TW.currentFontStack = () => TW.fontStack(TW.currentFont());

TW.applyTheme = function (id) {
    document.documentElement.setAttribute("data-theme", id);
    localStorage.setItem(TW.THEME_KEY, id);
    TW.renderThemeGrid();
    // Terminals recolor via the MutationObserver below.
};

TW.applyFont = function (id, {persist = true} = {}) {
    if (persist) localStorage.setItem(TW.FONT_KEY, id);

    const stack = TW.fontStack(id);
    document.documentElement.style.setProperty("--font-mono", stack);

    // Load the font, then apply to every open terminal and force a re-render.
    TW.ensureTermFont(id).then(() => {
        Object.values(TW.open).forEach(o => {
            o.term.options.fontFamily = stack;
            try {
                o.fit.fit();
                o.term.refresh(0, o.term.rows - 1);
            } catch (_) { /* ignore */ }
        });
    });

    const preview = TW.$("fontPreview");
    if (preview) preview.style.fontFamily = stack;
};

TW.renderThemeGrid = function () {
    const grid = TW.$("themeGrid");
    if (!grid) return;
    const active = localStorage.getItem(TW.THEME_KEY) || TW.DEFAULT_THEME;
    grid.innerHTML = "";
    TW.THEMES.forEach(t => {
        const card = document.createElement("div");
        card.className = "theme-card" + (t.id === active ? " active" : "");
        card.innerHTML = `
            <div class="theme-swatch theme-swatch-${t.id}">
                <span class="sw sw-bg"></span>
                <span class="sw sw-panel"></span>
                <span class="sw sw-accent"></span>
            </div>
            <div class="theme-card-foot">
                <span>${t.name}</span>
                <span class="material-icons">check_circle</span>
            </div>`;
        card.onclick = () => TW.applyTheme(t.id);
        grid.appendChild(card);
    });
};

TW.renderFontControls = function () {
    const sel = TW.$("fontSelect");
    if (!sel) return;
    const active = TW.currentFont();
    sel.innerHTML = "";
    TW.FONTS.forEach(f => {
        const opt = document.createElement("option");
        opt.value = f.id;
        opt.textContent = f.label;
        opt.selected = f.id === active;
        sel.appendChild(opt);
    });
    sel.onchange = () => TW.applyFont(sel.value);

    const preview = TW.$("fontPreview");
    if (preview) {
        preview.textContent =
            "switch# show version\n" +
            "abcdefghijklmnop  0123456789\n" +
            "=> {} [] () <> || && == != -> =~";
        preview.style.fontFamily = TW.currentFontStack();
    }
};

/* ===== xterm theming ===== */
TW.cssVar = (name, fb) =>
    getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fb;

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
    return {
        background: "rgba(0,0,0,0)",  // transparent — glass panel shows through
        foreground: TW.cssVar("--text-color", dark ? "#ffffff" : "#000000"),
        cursor: TW.cssVar("--text-color", dark ? "#ffffff" : "#000000"),
        cursorAccent: TW.cssVar("--surface", dark ? "#252526" : "#ffffff"),
        selectionBackground: TW.cssVar("--active-background", dark ? "#2f3b4a" : "#e3ecf7"),
        ...(dark ? ANSI_DARK : ANSI_LIGHT),
    };
};

TW.termOptions = function () {
    const fontSize = parseFloat(TW.cssVar("--font-size-sm", "13")) || 13;
    return {
        cursorBlink: true,
        fontFamily: TW.currentFontStack(),
        fontSize,
        lineHeight: 1.2,
        fontWeight: 400,
        allowProposedApi: true,
        allowTransparency: true,
        theme: TW.buildTheme(),
    };
};

TW.ensureTermFont = async function (fontId) {
    const fontSize = parseFloat(TW.cssVar("--font-size-sm", "13")) || 13;
    const family = fontId || TW.currentFont();
    if (document.fonts && family) {
        try {
            // Load both weights the terminal may request; quote multi-word names.
            await document.fonts.load(`400 ${fontSize}px "${family}"`);
            await document.fonts.ready;
        } catch (_) { /* ignore */ }
    }
};

TW.safeFit = function (id) {
    const o = TW.open[id];
    if (!o || o.wrap.offsetParent === null || o.wrap.clientHeight === 0) return;
    try {
        o.fit.fit();
        TW.socket.emit("resize", {session_id: id, cols: o.term.cols, rows: o.term.rows});
    } catch (_) { /* ignore */ }
};

// Recolor all terminals when the theme attribute flips.
new MutationObserver(() => {
    const theme = TW.buildTheme();
    Object.values(TW.open).forEach(o => {
        o.term.options.theme = theme;
        o.fit.fit();
    });
}).observe(document.documentElement, {attributes: true, attributeFilter: ["data-theme"]});

/* ===== Global UI wiring ===== */
// Backdrop-close for all modals.
document.querySelectorAll(".modal-overlay").forEach(ov => {
    ov.addEventListener("click", (e) => {
        if (e.target === ov) ov.classList.remove("open");
    });
});

/* ===== Boot: apply persisted appearance ===== */
(function boot() {
    TW.applyTheme(localStorage.getItem(TW.THEME_KEY) || TW.DEFAULT_THEME);
    TW.applyFont(TW.currentFont(), {persist: false});
})();

// Re-apply font once webfonts are ready (avoids first-paint fallback).
if (document.fonts?.ready) {
    document.fonts.ready.then(() => {
        const stack = TW.currentFontStack();
        Object.values(TW.open).forEach(o => { o.term.options.fontFamily = stack; });
        Object.keys(TW.open).forEach(id => TW.safeFit(id));
    });
}