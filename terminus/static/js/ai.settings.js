/* Terminus — AI provider settings form and connection test.

   The form is generated from the schema the server exposes at /api/ai/schema,
   so adding a provider needs no change here. Secrets arrive as booleans (true
   meaning "a value is stored"), never as values — submitting a blank secret
   keeps whatever is saved.

   The Assistant UI itself lives in ai.chat.js; this file is configuration only.
   Depends on core.js (TW namespace).

   File path: terminus/static/js/ai.settings.js */

"use strict";

(function () {
    const {$} = TW;

    const TEST_ICON = {
        ok: "check_circle",
        fail: "error",
        busy: "hourglass_top",
    };

    /* Stored config values are strings, so a checkbox round-trips as "1"/"0". */
    const TRUTHY = new Set(["1", "true", "yes", "on", "y"]);

    /* Ollama model families documented to support tool calling. Prefix match
       against the tag, so `qwen2.5:32b-instruct-q4_K_M` matches `qwen2.5`.
       Mirrors OllamaProvider._TOOL_FAMILIES — the server decides; this is only
       to give feedback before the user saves. */
    const TOOL_FAMILIES = [
        "llama3.1", "llama3.2", "llama3.3", "llama4",
        "qwen2.5", "qwen3", "qwq",
        "mistral-nemo", "mistral-large", "mistral-small", "devstral",
        "command-r", "command-a",
        "hermes3", "granite3", "granite4", "athene",
        "firefunction", "nemotron", "gpt-oss", "magistral", "cogito",
    ];

    /* Below this the nested command schema is unreliable in practice. Advisory,
       not a block. */
    const RECOMMENDED_PARAMS_B = 24;

    let schema = {};       // provider id -> {label, hint, fields[], capabilities}
    let settings = null;   // last payload from /api/ai/settings


    /* =====================================================================
       Schema and provider list
       ===================================================================== */
    async function loadSchema() {
        if (Object.keys(schema).length) return;
        try {
            const res = await fetch("/api/ai/schema");
            if (!res.ok) return;
            schema = (await res.json()).providers || {};
        } catch (err) {
            console.error("loadSchema failed:", err);
        }
    }

    function renderProviderOptions(selected) {
        const select = $("aiProvider");
        select.innerHTML = `<option value="">— Select provider —</option>`;

        Object.entries(schema).forEach(([id, spec]) => {
            const option = document.createElement("option");
            option.value = id;
            option.textContent = spec.label;      // textContent, never innerHTML
            option.selected = id === selected;
            select.appendChild(option);
        });
    }


    /* =====================================================================
       Capability note

       Azure's capability is fixed, so the schema answers it. Ollama's depends
       on a toggle and the model name, so it is predicted live as the user types
       rather than making them save to find out.
       ===================================================================== */
    function setNote(kind, html) {
        const note = $("aiCapabilityNote");
        if (!note) return;
        note.className = kind ? `cap-note cap-${kind}` : "form-hint";
        note.innerHTML = html;
    }

    function noteIcon(name) {
        return `<span class="material-icons i-16" aria-hidden="true">${name}</span>`;
    }

    /* Best-effort parameter count in billions, parsed from the tag. */
    function paramsInBillions(model) {
        const text = String(model || "").toLowerCase();

        let match = text.match(/(?:^|[:\-])(\d+(?:\.\d+)?)\s*b\b/);
        if (match) return parseFloat(match[1]);

        // Mixture-of-experts tags such as `8x7b` — the expert size is what
        // matters for instruction-following quality.
        match = text.match(/(\d+)x(\d+(?:\.\d+)?)b/);
        if (match) return parseFloat(match[2]);

        return null;
    }

    function renderSchemaCapability(provider) {
        const capabilities = schema[provider]?.capabilities;

        if (!provider || !capabilities) {
            setNote(null, "");
            return;
        }
        if (capabilities.supports_tools) {
            setNote("ok", `${noteIcon("check_circle")}
                <span>Supports the interactive Assistant.</span>`);
            return;
        }
        setNote("warn", `${noteIcon("info")}
            <span>Text generation only — the interactive Assistant needs tool
            calling, which is not enabled for this provider.</span>`);
    }

    function renderOllamaCapability() {
        const assistantOn = $("ai_assistant")?.checked;
        const model = ($("ai_model")?.value || "").trim();

        if (!assistantOn) {
            setNote("warn", `${noteIcon("info")}
                <span>Text generation only. Tick <strong>Enable the interactive
                Assistant</strong> to allow tool calling.</span>`);
            return;
        }
        if (!model) {
            setNote("warn", `${noteIcon("info")}
                <span>Enter a model to check tool-calling support.</span>`);
            return;
        }

        const family = model.toLowerCase().split(":")[0];
        const known = TOOL_FAMILIES.some((name) => family.startsWith(name));
        if (!known) {
            setNote("warn", `${noteIcon("warning")}
                <span><strong>${TW.esc(model)}</strong> is not a known
                tool-calling family, so the Assistant will stay disabled. Try
                qwen2.5, qwen3, llama3.3, mistral-nemo, command-r, hermes3 or
                granite3.</span>`);
            return;
        }

        const size = paramsInBillions(model);
        if (size !== null && size < RECOMMENDED_PARAMS_B) {
            setNote("warn", `${noteIcon("warning")}
                <span>Assistant enabled, but <strong>${TW.esc(model)}</strong>
                is about ${size}B parameters. The command schema is nested;
                models under ~${RECOMMENDED_PARAMS_B}B often propose malformed
                or wrong-platform commands. Every command is still
                policy-checked and needs your approval.</span>`);
            return;
        }

        setNote("ok", `${noteIcon("check_circle")}
            <span>Tool calling supported — the Assistant is available with
            <strong>${TW.esc(model)}</strong>.</span>`);
    }

    function renderCapability(provider) {
        if (provider === "ollama") renderOllamaCapability();
        else renderSchemaCapability(provider);
    }


    /* =====================================================================
       Config fields
       ===================================================================== */
    function fieldId(key) {
        return `ai_${key}`;
    }

    function hintHtml(field) {
        return field.hint
            ? `<span class="form-hint">${TW.esc(field.hint)}</span>`
            : "";
    }

    function checkboxValue(field, stored) {
        if (stored === undefined || stored === "") return !!field.default;
        return TRUTHY.has(String(stored).toLowerCase());
    }

    function renderCheckboxField(field, stored, provider) {
        const id = fieldId(field.key);
        const row = document.createElement("label");
        row.className = "toggle-row ai-toggle-row";
        row.innerHTML = `
            <span class="toggle-text">
                <span class="toggle-title">${TW.esc(field.label)}</span>
                ${hintHtml(field)}
            </span>
            <input type="checkbox" id="${TW.esc(id)}" class="switch"/>`;

        $("aiConfigFields").appendChild(row);
        $(id).checked = checkboxValue(field, stored);
        $(id).onchange = () => renderCapability(provider);
    }

    function renderTextField(field, stored, provider) {
        const id = fieldId(field.key);
        // A stored secret is reported as `true`, never as its value.
        const secretIsSet = field.secret && stored === true;
        const placeholder = secretIsSet
            ? "Saved — blank keeps it"
            : (field.placeholder || "");

        const row = document.createElement("div");
        row.className = "form-row";
        row.innerHTML = `
            <label for="${TW.esc(id)}">
                ${TW.esc(field.label)}${field.required ? "" : " (optional)"}
            </label>
            <input id="${TW.esc(id)}" type="${TW.esc(field.type || "text")}"
                   autocomplete="off" placeholder="${TW.esc(placeholder)}"/>
            ${hintHtml(field)}`;

        $("aiConfigFields").appendChild(row);

        if (!field.secret && typeof stored === "string") {
            $(id).value = stored;
        }
        if (field.key === "model") {
            $(id).oninput = () => renderCapability(provider);
        }
    }

    function renderConfigFields(provider, values = {}) {
        const host = $("aiConfigFields");
        const spec = schema[provider];

        $("aiProviderHint").textContent = spec?.hint || "";
        $("aiConfigGroup").style.display = spec ? "" : "none";
        host.innerHTML = "";

        if (!spec) {
            renderCapability(provider);
            return;
        }

        spec.fields.forEach((field) => {
            const stored = values[field.key];
            if (field.type === "checkbox") {
                renderCheckboxField(field, stored, provider);
            } else {
                renderTextField(field, stored, provider);
            }
        });

        // After the fields exist, so the Ollama path can read them.
        renderCapability(provider);
    }

    function configPayload() {
        const provider = $("aiProvider").value;
        const spec = schema[provider];
        if (!spec) return {};

        const config = {};
        spec.fields.forEach((field) => {
            const el = $(fieldId(field.key));
            if (!el) return;
            config[field.key] = field.type === "checkbox"
                ? (el.checked ? "1" : "0")
                : el.value.trim();
        });
        return config;
    }


    /* =====================================================================
       Load and save
       ===================================================================== */
    TW.loadAISettings = async function () {
        await loadSchema();

        try {
            const res = await fetch("/api/ai/settings");
            settings = res.ok ? await res.json() : null;
            if (!res.ok) {
                TW.toast(await TW.errText(res, "Could not load AI settings."));
            }
        } catch (err) {
            console.error("loadAISettings failed:", err);
            TW.toast("Could not load AI settings.");
            settings = null;
        }

        const provider = settings?.provider || "";
        $("aiEnabled").checked = !!settings?.enabled;
        renderProviderOptions(provider);
        renderConfigFields(provider, settings?.config || {});
        hideTestResult();
    };

    /* Reuse stored values only when re-selecting the saved provider — another
       provider's config would populate the wrong fields. */
    $("aiProvider").onchange = () => {
        const provider = $("aiProvider").value;
        const values = provider === settings?.provider
            ? (settings.config || {})
            : {};
        renderConfigFields(provider, values);
        hideTestResult();
    };

    async function saveSettings(extra = {}) {
        const body = {
            provider: $("aiProvider").value,
            config: configPayload(),
            ...extra,
        };

        try {
            const res = await TW.postJSON("/api/ai/settings", body);
            if (!res.ok) {
                TW.toast(await TW.errText(res, "Could not save AI settings."));
                return false;
            }
            // Reload rather than trusting local state: the server decides
            // whether the result is actually active and tool-capable.
            await TW.loadAISettings();
            await TW.refreshAIState();
            return true;
        } catch (err) {
            console.error("saveSettings failed:", err);
            TW.toast("Could not save AI settings.");
            return false;
        }
    }

    $("aiSaveBtn").onclick = async () => {
        if (await saveSettings()) TW.toast("AI settings saved.");
    };


    /* =====================================================================
       Enable toggle → disclaimer

       Enabling always re-shows the disclaimer. The switch is reverted until the
       user accepts, so the visible state never claims more than they agreed to.
       ===================================================================== */
    $("aiEnabled").onchange = async () => {
        if (!$("aiEnabled").checked) {
            if (await saveSettings({enabled: false})) {
                TW.toast("AI features disabled.");
            }
            return;
        }
        $("aiEnabled").checked = false;
        TW.openModal("aiDisclaimerModal");
    };

    $("aiDeclineBtn").onclick = () => TW.closeModal("aiDisclaimerModal");

    $("aiAcceptBtn").onclick = async () => {
        TW.closeModal("aiDisclaimerModal");
        // disclaimer_ok records acceptance of the *current* disclaimer version;
        // a bump in DISCLAIMER_VERSION forces re-consent.
        if (await saveSettings({enabled: true, disclaimer_ok: true})) {
            TW.toast("AI features enabled.");
        }
    };


    /* =====================================================================
       Provider test

       Really contacts the provider — for Azure that includes a one-token
       completion, so a wrong deployment name fails here rather than at first
       use.
       ===================================================================== */
    function showTestResult(kind, message) {
        const el = $("aiTestResult");
        el.className = `test-result show ${kind}`;
        // The message is provider- or exception-authored — escape it.
        el.innerHTML = `${noteIcon(TEST_ICON[kind])}` +
            `<span>${TW.esc(message)}</span>`;
    }

    function hideTestResult() {
        $("aiTestResult").className = "test-result";
    }

    $("aiTestBtn").onclick = async () => {
        const provider = $("aiProvider").value;
        if (!provider) {
            TW.toast("Select a provider first.");
            $("aiProvider").focus();
            return;
        }

        showTestResult("busy", "Contacting provider…");
        try {
            // Blank secrets fall back to stored values server-side, so the user
            // can test without re-entering credentials.
            const res = await TW.postJSON("/api/ai/test",
                {provider, config: configPayload()});
            const data = await res.json().catch(() => ({}));
            showTestResult(
                res.ok && data.ok ? "ok" : "fail",
                data.message || (res.ok ? "Test failed."
                    : `Request failed: ${res.status}`),
            );
        } catch (err) {
            console.error("aiTest failed:", err);
            showTestResult("fail", "Request failed.");
        }
    };
})();