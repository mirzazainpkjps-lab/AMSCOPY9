(function () {
    var STORAGE_KEY = "ams_theme";
    var VALID = { light: true, dark: true };

    function sanitize(mode) {
        var m = String(mode || "").toLowerCase();
        return VALID[m] ? m : null;
    }

    function systemPrefersDark() {
        return !!(window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches);
    }

    function resolveInitialTheme(serverPreferred) {
        var saved = null;
        try {
            saved = sanitize(localStorage.getItem(STORAGE_KEY));
        } catch (_) {
            saved = null;
        }
        if (saved) return saved;
        var fromServer = sanitize(serverPreferred || document.documentElement.getAttribute("data-server-theme"));
        if (fromServer) return fromServer;
        return systemPrefersDark() ? "dark" : "light";
    }

    function applyTheme(mode) {
        var theme = sanitize(mode) || "dark";
        document.documentElement.setAttribute("data-theme", theme);
        document.documentElement.style.colorScheme = theme;
        document.body && document.body.setAttribute("data-theme", theme);
        return theme;
    }

    function iconFor(mode) {
        return mode === "dark" ? "bi-moon-stars-fill" : "bi-sun-fill";
    }

    function labelFor(mode) {
        return mode === "dark" ? "Dark" : "Light";
    }

    function updateToggleButton(btn, mode) {
        if (!btn) return;
        var icon = btn.querySelector("i");
        if (icon) {
            icon.className = "bi " + iconFor(mode);
        }
        var label = btn.querySelector(".theme-label");
        if (label) {
            label.textContent = labelFor(mode);
        }
        btn.setAttribute("aria-label", "Switch theme. Current: " + labelFor(mode));
        btn.setAttribute("title", "Theme: " + labelFor(mode));
    }

    async function persistServer(theme) {
        try {
            await fetch("/api/ui/theme", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                credentials: "same-origin",
                body: JSON.stringify({ theme: theme })
            });
        } catch (_) {}
    }

    function setTheme(mode, persist) {
        var theme = applyTheme(mode);
        try {
            localStorage.setItem(STORAGE_KEY, theme);
        } catch (_) {}
        var buttons = document.querySelectorAll("[data-theme-toggle]");
        buttons.forEach(function (btn) { updateToggleButton(btn, theme); });
        if (persist) persistServer(theme);
        document.dispatchEvent(new CustomEvent("ams:themechange", { detail: { theme: theme } }));
        return theme;
    }

    function toggleTheme() {
        var current = document.documentElement.getAttribute("data-theme") || "dark";
        return setTheme(current === "dark" ? "light" : "dark", true);
    }

    function bindToggles() {
        document.querySelectorAll("[data-theme-toggle]").forEach(function (btn) {
            if (btn.dataset.themeBound === "1") return;
            btn.dataset.themeBound = "1";
            btn.addEventListener("click", function (e) {
                e.preventDefault();
                toggleTheme();
            });
        });
    }

    function init(serverPreferred) {
        var initial = resolveInitialTheme(serverPreferred);
        setTheme(initial, false);
        bindToggles();
    }

    window.AMSTheme = {
        init: init,
        setTheme: function (mode) { return setTheme(mode, true); },
        toggleTheme: toggleTheme,
        getTheme: function () { return document.documentElement.getAttribute("data-theme") || "dark"; }
    };
})();
