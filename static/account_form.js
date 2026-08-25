/*
 * Account Create/Edit form engine.
 *
 * Drives the controlled Category → Subcategory → Account Type → Channel →
 * linked-entity cascade from the classification registry, reveals only the
 * channel-specific detail fields that are relevant, and keeps the live preview
 * in sync.  Used by both add_account.html and edit_account.html so the two
 * forms behave identically.
 *
 * The page is expected to define:
 *   window.ACCOUNT_REGISTRY   - registry projection (see classification.registry_json)
 *   window.ACCOUNT_FORM_MODE  - 'create' | 'edit'
 *   window.ACCOUNT_PRESET     - optional object of preselected values (edit)
 */
(function () {
    'use strict';

    var REGISTRY = window.ACCOUNT_REGISTRY || { categories: [] };
    var MODE = window.ACCOUNT_FORM_MODE || 'create';
    var PRESET = window.ACCOUNT_PRESET || {};

    var fmtMoney = function (n) {
        // FIX: Validate parseFloat results — NaN must never propagate
        var rawStr = String(n == null ? '0' : n).trim().replace(/,/g, '');
        var v = parseFloat(rawStr);
        if (isNaN(v) || !isFinite(v)) v = 0;
        return 'Rs. ' + v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    };

    function $(id) { return document.getElementById(id); }
    function clear(sel) { while (sel.firstChild) sel.removeChild(sel.firstChild); }
    function addOpt(sel, value, label, selected) {
        var o = document.createElement('option');
        o.value = value;
        o.textContent = label;
        if (selected) o.selected = true;
        sel.appendChild(o);
    }

    // ---- registry lookups -------------------------------------------------
    function findCategory(name) {
        for (var i = 0; i < REGISTRY.categories.length; i++) {
            if (REGISTRY.categories[i].name === name) return REGISTRY.categories[i];
        }
        return null;
    }
    function findSub(cat, name) {
        if (!cat) return null;
        for (var i = 0; i < cat.subcategories.length; i++) {
            if (cat.subcategories[i].name === name) return cat.subcategories[i];
        }
        return null;
    }
    function findType(sub, name) {
        if (!sub) return null;
        for (var i = 0; i < sub.account_types.length; i++) {
            if (sub.account_types[i].name === name) return sub.account_types[i];
        }
        return null;
    }

    // ---- cascading builders ----------------------------------------------
    function buildCategories(selected) {
        var sel = $('class_category');
        clear(sel);
        addOpt(sel, '', 'Select category', false);
        REGISTRY.categories.forEach(function (c) {
            addOpt(sel, c.name, c.label || c.name, c.name === selected);
        });
        var desc = findCategory(sel.value);
        setText('cat_desc', desc ? desc.description : '');
    }

    function buildSubcategories(catName, selected) {
        var sel = $('class_subcategory');
        clear(sel);
        addOpt(sel, '', 'Select subcategory', false);
        var cat = findCategory(catName);
        if (cat) {
            cat.subcategories.forEach(function (s) {
                addOpt(sel, s.name, s.label || s.name, s.name === selected);
            });
        }
    }

    function buildAccountTypes(catName, subName, selected) {
        var sel = $('class_account_type');
        clear(sel);
        addOpt(sel, '', 'Select account type', false);
        var sub = findSub(findCategory(catName), subName);
        if (sub) {
            sub.account_types.forEach(function (t) {
                addOpt(sel, t.name, t.name, t.name === selected);
            });
        }
    }

    // ---- channel + entity + details --------------------------------------
    function currentNode() {
        var cat = $('class_category').value;
        var sub = $('class_subcategory').value;
        var typ = $('class_account_type').value;
        return findType(findSub(findCategory(cat), sub), typ);
    }

    function renderChannel(node, preserve) {
        var sel = $('channel');
        var wrap = $('channel_wrap');
        if (!node) {
            wrap.style.display = 'none';
            return;
        }
        var forced = node.channels.length === 1;
        if (forced) {
            // Auto-set, not user-selectable.
            wrap.style.display = 'none';
            clear(sel);
            addOpt(sel, node.default_channel, channelLabel(node.default_channel), true);
            sel.value = node.default_channel;
        } else {
            wrap.style.display = '';
            clear(sel);
            node.channels.forEach(function (ch) {
                addOpt(sel, ch, channelLabel(ch), ch === node.default_channel);
            });
            if (preserve && node.channels.indexOf(preserve) >= 0) {
                sel.value = preserve;
            }
        }
        renderDetails(sel.value);
    }

    function channelLabel(ch) {
        var map = {
            cash: 'Cash', bank: 'Bank', digital_wallet: 'Digital Wallet',
            ledger_only: 'Ledger Only', other: 'Other'
        };
        return map[ch] || ch;
    }

    function renderDetails(channel) {
        ['cash_details', 'bank_details', 'wallet_details'].forEach(function (id) {
            var el = $(id);
            if (el) el.style.display = 'none';
        });
        if (channel === 'cash' && $('cash_details')) $('cash_details').style.display = '';
        else if (channel === 'bank' && $('bank_details')) $('bank_details').style.display = '';
        else if (channel === 'digital_wallet' && $('wallet_details')) $('wallet_details').style.display = '';
    }

    function renderEntity(node) {
        var wrap = $('linked_entity_wrap');
        var type = node ? node.entity : 'none';
        var typeInput = $('linked_entity_type');
        if (typeInput) typeInput.value = type;
        if (!wrap) return;
        if (!type || type === 'none') {
            wrap.style.display = 'none';
            return;
        }
        wrap.style.display = '';
        // Toggle the specific entity input group.
        ['entity_client', 'entity_supplier', 'entity_party'].forEach(function (id) {
            var el = $(id);
            if (el) el.style.display = 'none';
        });
        var labelEl = $('linked_entity_label');
        var labels = { client: 'Linked Client', supplier: 'Linked Supplier',
            partner: 'Linked Partner', worker: 'Linked Worker',
            vehicle: 'Linked Vehicle', party: 'Linked Party' };
        if (labelEl) labelEl.textContent = labels[type] || 'Linked Party';

        if (type === 'client' && $('entity_client')) $('entity_client').style.display = '';
        else if (type === 'supplier' && $('entity_supplier')) $('entity_supplier').style.display = '';
        else if ($('entity_party')) $('entity_party').style.display = '';
    }

    // ---- live preview -----------------------------------------------------
    function setText(id, t) { var el = $(id); if (el) el.textContent = (t === '' || t == null) ? '—' : t; }
    function updatePreview() {
        setText('pv_name', val('name'));
        setText('pv_category', val('class_category'));
        setText('pv_subcategory', val('class_subcategory'));
        setText('pv_account_type', val('class_account_type'));
        var ch = val('channel');
        setText('pv_channel', channelLabel(ch));
        // channel-specific preview rows
        setText('pv_bank', ch === 'bank' ? val('bank_name') : '');
        setText('pv_acct', ch === 'bank' ? val('account_number') : '');
        setText('pv_wallet', ch === 'digital_wallet' ? (val('wallet_provider') + ' ' + val('wallet_number')).trim() : '');
        setText('pv_cashloc', ch === 'cash' ? val('cash_location') : '');
        // linked entity preview
        var node = currentNode();
        var entLabel = '—';
        if (node && node.entity && node.entity !== 'none') {
            if (node.entity === 'client') {
                var cs = $('linked_client_id');
                entLabel = cs && cs.selectedOptions[0] ? cs.selectedOptions[0].textContent.trim() : (PRESET.linked_party_name || '—');
            } else if (node.entity === 'supplier') {
                var ss = $('linked_supplier_id');
                entLabel = ss && ss.selectedOptions[0] ? ss.selectedOptions[0].textContent.trim() : (PRESET.linked_party_name || '—');
            } else {
                entLabel = val('linked_party_name') || '—';
            }
        }
        setText('pv_entity', entLabel);
        setText('pv_status', statusLabel(val('account_status')));
        var amt = parseFloat(val('opening_amount') || '0') || 0;
        var pos = (radioVal('opening_position') || 'debit');
        var signed = pos === 'credit' ? -amt : amt;
        setText('pv_opening', fmtMoney(signed));
        setText('pv_opening_date', val('opening_effective_date'));
    }

    function signedOpening() {
        var amt = parseFloat(val('opening_amount') || '0') || 0;
        var pos = (radioVal('opening_position') || 'debit');
        return pos === 'credit' ? -amt : amt;
    }

    function statusLabel(s) {
        var map = { active: 'Active', inactive: 'Inactive', archived: 'Archived' };
        return map[s] || s || '—';
    }
    function val(id) {
        var el = $(id);
        if (!el) return '';
        var raw = (el.value || '').trim();
        // FIX: Reject clearly invalid numeric input; treat empty string as '' (not 0)
        if (raw === '' || raw === '-') return '';
        var parsed = parseFloat(raw);
        if (isNaN(parsed) || !isFinite(parsed)) return '';
        return raw;
    }
    function radioVal(name) {
        var checked = document.querySelector('input[name="' + name + '"]:checked');
        return checked ? (checked.value || '').trim() : '';
    }

    // ---- edit-only opening + adjustment calculator -----------------------
    // Changing the opening baseline shifts today's calculated balance by the
    // same amount. Preserve any adjustment gap the user already typed.
    var originalOpening = parseFloat(PRESET.original_opening);
    if (isNaN(originalOpening)) {
        originalOpening = parseFloat($('original_opening_hidden') ? $('original_opening_hidden').value : 0) || 0;
    }
    var originalCurrent = parseFloat(PRESET.original_current);
    if (isNaN(originalCurrent)) {
        originalCurrent = parseFloat($('current_balance_hidden') ? $('current_balance_hidden').value : 0) || 0;
    }
    // Gap the user wants between calculated current and desired closing.
    // Opening edits change current; desired follows so only a typed mismatch
    // becomes an Adjustment.
    var adjustmentGap = 0;

    function effectiveCurrent() {
        return originalCurrent + (signedOpening() - originalOpening);
    }

    function captureAdjustmentGap() {
        var desEl = $('desired_balance');
        if (!desEl) return;
        var des = parseFloat(desEl.value);
        if (isNaN(des)) des = effectiveCurrent();
        adjustmentGap = des - effectiveCurrent();
    }

    function applyOpeningShiftToDesired() {
        var desEl = $('desired_balance');
        if (!desEl) return;
        desEl.value = (effectiveCurrent() + adjustmentGap).toFixed(2);
    }

    function updateAdjustment() {
        if (MODE !== 'edit') return;
        var cur = effectiveCurrent();
        var hid = $('current_balance_hidden');
        if (hid) hid.value = cur.toFixed(2);
        var curDisp = $('current_balance_display');
        if (curDisp) curDisp.textContent = fmtMoney(cur);
        var desEl = $('desired_balance');
        var des = desEl ? (parseFloat(desEl.value) || 0) : cur;
        var diff = des - cur;
        var diffEl = $('adj_diff');
        if (diffEl) {
            diffEl.textContent = (diff >= 0 ? '+' : '−') + fmtMoney(Math.abs(diff)).replace('Rs. ', 'Rs. ');
            diffEl.className = 'adj-diff ' + (diff > 0 ? 'pos' : (diff < 0 ? 'neg' : 'zero'));
        }
        var dirEl = $('adj_direction');
        if (dirEl) dirEl.textContent = diff === 0 ? 'No change' : (diff > 0 ? 'Increase' : 'Decrease');
        var newEl = $('adj_new_balance');
        if (newEl) newEl.textContent = fmtMoney(des);
        // Reason is required only when an adjustment is actually happening.
        var reasonWrap = $('adjustment_reason_wrap');
        if (reasonWrap) reasonWrap.style.display = (Math.abs(diff) < 0.01) ? 'none' : '';  // FIX: use stable threshold 0.01 instead of unstable 0.005
        var previewCur = $('pv_current_balance');
        if (previewCur) previewCur.textContent = fmtMoney(cur);
        var previewNew = $('pv_new_balance');
        if (previewNew) previewNew.textContent = fmtMoney(des);
        var previewDiff = $('pv_adjustment');
        if (previewDiff) {
            previewDiff.textContent = diff === 0 ? 'No adjustment' : ((diff > 0 ? '+' : '−') + fmtMoney(Math.abs(diff)));
        }
    }

    function refreshAllFromType() {
        var node = currentNode();
        renderChannel(node, val('channel'));
        renderEntity(node);
        updatePreview();
    }

    // ---- init -------------------------------------------------------------
    function init() {
        // Initial category list, then cascade to preset values (edit mode).
        var preCat = PRESET.class_category || '';
        var preSub = PRESET.class_subcategory || '';
        var preType = PRESET.class_account_type || '';
        buildCategories(preCat);
        if (preCat) {
            buildSubcategories(preCat, preSub);
            if (preSub) {
                buildAccountTypes(preCat, preSub, preType);
            }
        }
        renderChannel(currentNode(), PRESET.channel);
        renderEntity(currentNode());
        updatePreview();
        updateAdjustment();

        // Event wiring
        var catEl = $('class_category');
        if (catEl) catEl.addEventListener('change', function () {
            buildSubcategories(catEl.value, '');
            buildAccountTypes(catEl.value, '', '');
            refreshAllFromType();
        });
        var subEl = $('class_subcategory');
        if (subEl) subEl.addEventListener('change', function () {
            buildAccountTypes($('class_category').value, subEl.value, '');
            refreshAllFromType();
        });
        var typeEl = $('class_account_type');
        if (typeEl) typeEl.addEventListener('change', refreshAllFromType);
        var chEl = $('channel');
        if (chEl) chEl.addEventListener('change', function () { renderDetails(chEl.value); updatePreview(); });

        // Preview-affecting inputs
        ['name', 'opening_amount', 'opening_effective_date',
         'cash_location', 'bank_name', 'account_number', 'wallet_provider',
         'wallet_number', 'linked_party_name', 'account_status', 'desired_balance']
            .forEach(function (id) {
                var el = $(id);
                if (!el) return;
                el.addEventListener('input', function () {
                    if (id === 'desired_balance') captureAdjustmentGap();
                    if (id === 'opening_amount') applyOpeningShiftToDesired();
                    updatePreview();
                    if (id === 'desired_balance' || id === 'opening_amount') updateAdjustment();
                });
                el.addEventListener('change', function () {
                    if (id === 'desired_balance') captureAdjustmentGap();
                    if (id === 'opening_amount') applyOpeningShiftToDesired();
                    updatePreview();
                    if (id === 'desired_balance' || id === 'opening_amount') updateAdjustment();
                });
            });
        document.querySelectorAll('input[name="opening_position"]').forEach(function (radio) {
            radio.addEventListener('change', function () {
                document.querySelectorAll('.pos-option').forEach(function (opt) {
                    var inp = opt.querySelector('input[name="opening_position"]');
                    opt.classList.toggle('selected', !!(inp && inp.checked));
                });
                applyOpeningShiftToDesired();
                updatePreview();
                updateAdjustment();
            });
        });
        ['linked_client_id', 'linked_supplier_id'].forEach(function (id) {
            var el = $(id);
            if (el) el.addEventListener('change', updatePreview);
        });

        // Adjustment reason "Other" reveals the custom text box.
        var reasonEl = $('adjustment_reason');
        if (reasonEl) reasonEl.addEventListener('change', function () {
            var otherWrap = $('adjustment_reason_other_wrap');
            if (otherWrap) otherWrap.style.display = (reasonEl.value === 'Other') ? '' : 'none';
        });

        // Double-submit protection (PART 14): disable the submit button during
        // submission so a retry/double-click cannot post twice.
        var form = document.getElementById('accountForm');
        if (form) {
            form.addEventListener('submit', function () {
                var btn = document.getElementById('accountSubmit');
                if (btn) {
                    btn.disabled = true;
                    btn.classList.add('submitting');
                    var orig = btn.innerHTML;
                    btn.dataset.origHtml = orig;
                    btn.innerHTML = '<i class="bi bi-hourglass-split me-1"></i> Saving…';
                    // Re-enable after a timeout in case validation rejects the
                    // submit server-side (full page reload handles the success
                    // path; this only covers an aborted/navigation case).
                    setTimeout(function () { if (btn) { btn.disabled = false; btn.classList.remove('submitting'); if (btn.dataset.origHtml) btn.innerHTML = btn.dataset.origHtml; } }, 6000);
                }
            });
        }

        // Show "Other" reason box on load if preset.
        if (PRESET.adjustment_reason === 'Other' && $('adjustment_reason_other_wrap')) {
            $('adjustment_reason_other_wrap').style.display = '';
        }
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
