/* AMS UI v2 — runtime helpers (side-sheet, flatpickr defaults, theme-aware combobox).
   Loaded after theme.js. Safe to load on legacy pages — features are opt-in. */
(function () {
    "use strict";

    /* -----------------------------------------------------------------
       Side-sheet drawer (replaces fullscreen modal forms)
       Markup contract:
         <div class="ui-sheet" id="someSheet" data-ui-sheet>
           <div class="ui-sheet-header">…<button data-ui-sheet-close></button></div>
           <div class="ui-sheet-body">…</div>
           <div class="ui-sheet-footer">…</div>
         </div>
       Open with: AMSUI.openSheet('someSheet')   or
                  <button data-ui-sheet-target="someSheet">…</button>
       ----------------------------------------------------------------- */
    var backdrop = null;
    function ensureBackdrop() {
        if (backdrop) return backdrop;
        backdrop = document.createElement("div");
        backdrop.className = "ui-sheet-backdrop";
        backdrop.addEventListener("click", closeAllSheets);
        document.body.appendChild(backdrop);
        return backdrop;
    }
    function openSheet(idOrEl) {
        var el = (typeof idOrEl === "string") ? document.getElementById(idOrEl) : idOrEl;
        if (!el) return;
        var bd = ensureBackdrop();
        bd.classList.add("show");
        el.classList.add("show");
        document.body.style.overflow = "hidden";
        el.dispatchEvent(new CustomEvent("ams:sheetopen"));
    }
    function closeSheet(idOrEl) {
        var el = (typeof idOrEl === "string") ? document.getElementById(idOrEl) : idOrEl;
        if (!el) return;
        el.classList.remove("show");
        var anyOpen = document.querySelector(".ui-sheet.show");
        if (!anyOpen && backdrop) {
            backdrop.classList.remove("show");
            document.body.style.overflow = "";
        }
        el.dispatchEvent(new CustomEvent("ams:sheetclose"));
    }
    function closeAllSheets() {
        document.querySelectorAll(".ui-sheet.show").forEach(function (el) { el.classList.remove("show"); });
        if (backdrop) backdrop.classList.remove("show");
        document.body.style.overflow = "";
    }

    document.addEventListener("click", function (ev) {
        var openBtn = ev.target.closest("[data-ui-sheet-target]");
        if (openBtn) {
            ev.preventDefault();
            openSheet(openBtn.getAttribute("data-ui-sheet-target"));
            return;
        }
        var closeBtn = ev.target.closest("[data-ui-sheet-close]");
        if (closeBtn) {
            ev.preventDefault();
            var sheet = closeBtn.closest(".ui-sheet");
            if (sheet) closeSheet(sheet);
        }
    });
    document.addEventListener("keydown", function (ev) {
        if (ev.key === "Escape") closeAllSheets();
    });

    /* -----------------------------------------------------------------
       Flatpickr unified defaults (only on .ui-date / .ui-datetime inputs)
       ----------------------------------------------------------------- */
    function initDatePickers(root) {
        if (typeof flatpickr === "undefined") return;
        (root || document).querySelectorAll(".ui-date:not(.ui-pk-bound)").forEach(function (el) {
            el.classList.add("ui-pk-bound");
            flatpickr(el, { dateFormat: "Y-m-d", allowInput: true });
        });
        (root || document).querySelectorAll(".ui-datetime:not(.ui-pk-bound)").forEach(function (el) {
            el.classList.add("ui-pk-bound");
            flatpickr(el, { enableTime: true, dateFormat: "Y-m-d H:i", allowInput: true });
        });
    }

    /* -----------------------------------------------------------------
       Theme-aware combobox skin (adds ui-combobox-v2 class to existing
       .combobox-list elements on opted-in pages so theme tokens apply)
       ----------------------------------------------------------------- */
    function skinCombos(root) {
        (root || document).querySelectorAll(".ui-v2 .combobox-list").forEach(function (el) {
            el.classList.add("ui-combobox-v2");
        });
    }

    /* -----------------------------------------------------------------
       Row-click drill-down: any <tr data-href="..."> becomes navigable
       ----------------------------------------------------------------- */
    function bindRowDrill(root) {
        (root || document).querySelectorAll("tr[data-href]:not(.ui-row-bound)").forEach(function (tr) {
            tr.classList.add("ui-row-bound", "is-clickable");
            tr.addEventListener("click", function (ev) {
                if (ev.target.closest("a, button, input, label, select, textarea")) return;
                window.location.href = tr.getAttribute("data-href");
            });
        });
    }

    function initAll(root) {
        initDatePickers(root);
        skinCombos(root);
        bindRowDrill(root);
    }

    document.addEventListener("DOMContentLoaded", function () { initAll(document); });

    window.AMSUI = {
        openSheet: openSheet,
        closeSheet: closeSheet,
        closeAllSheets: closeAllSheets,
        initAll: initAll,
        initDatePickers: initDatePickers
    };

    function findMaterialIdInput(input) {
        if (!input) return null;
        var named = input.getAttribute("data-material-id-input") || "";
        if (named && document.getElementById(named)) return document.getElementById(named);
        var wrap = input.closest(".position-relative") || input.closest(".compact-item-row") || input.closest(".return-row") || input.parentElement;
        if (!wrap) return null;
        return wrap.querySelector('input[name="material_id[]"], input[name="alternate_material_id[]"], input[name="material_id"]');
    }
    window.setSelectedMaterialId = function (input, materialId, selectedName) {
        if (!input) return;
        var hid = findMaterialIdInput(input);
        if (hid) hid.value = materialId || "";
        if (materialId) {
            input.dataset.selectedMaterialId = String(materialId);
            input.dataset.selectedMaterialName = selectedName || input.value || "";
        } else {
            delete input.dataset.selectedMaterialId;
            delete input.dataset.selectedMaterialName;
        }
    };
    window.clearSelectedMaterialId = function (input) {
        window.setSelectedMaterialId(input, "", "");
    };
    document.addEventListener("input", function (e) {
        var input = e.target && e.target.closest
            ? e.target.closest('input[name="product_name[]"], input[name="material_name[]"], input[name="alternate_material[]"], input[name="material"]')
            : null;
        if (!input) return;
        if (input.dataset.comboSuppress === "1") return;
        var selectedName = (input.dataset.selectedMaterialName || "").trim().toLowerCase();
        var current = (input.value || "").trim().toLowerCase();
        if (!selectedName || current !== selectedName) {
            window.clearSelectedMaterialId(input);
        }
    });
    document.addEventListener("submit", function (e) {
        var form = e.target;
        if (!form || form.tagName !== "FORM") return;
        if (form.dataset.skipValidation === "1") return;
        var rows = form.querySelectorAll('input[name="product_name[]"], input[name="material_name[]"], input[name="material"]');
        if (!rows.length) return;
        var issues = [];
        rows.forEach(function (input, idx) {
            var typed = (input.value || "").trim();
            if (!typed) return;
            var hid = findMaterialIdInput(input);
            var selectedId = (hid && hid.value) || input.dataset.selectedMaterialId || "";
            var selectedName = (input.dataset.selectedMaterialName || "").trim();
            if (selectedId && selectedName && selectedName.toLowerCase() === typed.toLowerCase()) return;
            if (selectedId && !selectedName) return;
            var known = window.AMS_KNOWN_MATERIAL_NAMES;
            if (known && (known.has(typed) || known.has(typed.toLowerCase()))) return;
            issues.push(idx + 1);
        });
        if (issues.length) {
            e.preventDefault();
            e.stopImmediatePropagation();
            alert("Material not selected. Please select an existing material from the Material Master.");
        }
    }, true);
})();
