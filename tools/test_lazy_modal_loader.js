/* Surgical state-machine test for static/lazy_modal_loader.js (no browser).
   Stubs a minimal DOM (no HTML parsing — querySelector returns pre-built
   stub nodes) and verifies the behaviours that matter for "no stuck overlay":
     1. success opens the modal + frees the lock
     2. double-click while in flight -> exactly ONE fetch
     3. abort (timeout) -> error dialog, lock HELD, Edit ignored
     4. Retry -> one new fetch, success opens the modal
     5. closing the error dialog -> lock freed, a fresh Edit works
*/
"use strict";
const fs = require('fs');
const assert = require('assert');
const vm = require('vm');

/* ---------- tiny stub element (no HTML parsing) ---------- */
function makeStub() {
    const el = {
        _handlers: {},
        _innerHTML: '',
        children: [],
        style: {},
        dataset: {},
        ownerDocument: null,
        addEventListener(ev, fn) { (this._handlers[ev] = this._handlers[ev] || []).push(fn); },
        removeEventListener() {},
        fire(ev) { (this._handlers[ev] || []).slice().forEach(fn => fn()); },
        set innerHTML(v) { this._innerHTML = v; },
        get innerHTML() { return this._innerHTML; },
        replaceChildren(...c) { this.children = c; },
        querySelector() { return this._child; },
        contains() { return true; },
        appendChild(c) { this.children.push(c); return c; },
        firstElementChild: null,
    };
    return el;
}

/* The loader queries specific nodes by selector after setting innerHTML.
   We return the same pre-built stub for any selector on the loader shell,
   and a dedicated "state" stub whose innerHTML we can inspect. */
const stateStub = makeStub();            // #amsLazyLoaderState
const shellStub = makeStub();            // #amsLazyModalLoader
shellStub.querySelector = (sel) => (sel === '#amsLazyLoaderState' ? stateStub : stateStub);
// errorState() reads two buttons; give it stub buttons that record handlers.
const retryBtn = makeStub();
const cancelBtn = makeStub();
let errorDivStub = null;
const documentStub = {
    body: makeStub(),
    _created: [],
    createElement() {
        const e = makeStub();
        documentStub._created.push(e);
        return e;
    },
    getElementById(id) {
        if (id === 'amsLazyModalLoader') return shellStub;
        if (id === 'amsLazyLoaderState') return stateStub;
        if (id === 'lazyEditSaleModalHost') return hostStub;
        return null;
    },
    addEventListener() {},
};
// errorState's div.querySelector('.btn-warning' | '.btn-outline-secondary')
// We patch makeStub for the error div when it is created.
const origCreate = documentStub.createElement.bind(documentStub);
documentStub.createElement = function () {
    const e = origCreate();
    e.querySelector = (sel) => (sel === '.btn-warning' ? retryBtn : sel === '.btn-outline-secondary' ? cancelBtn : null);
    e.firstElementChild = shellStub;   // ensureLoaderModal reads wrap.firstElementChild
    return e;
};
/* Error text is set on a child via replaceChildren(); spinner text is set as
   innerHTML directly. Read whichever is present. */
function stateText() {
    const c = stateStub.children[stateStub.children.length - 1];
    return (c && c._innerHTML) ? c._innerHTML : stateStub._innerHTML;
}

/* host + the "real" modal that the fragment is said to contain */
let realModalStub = null;
const hostStub = makeStub();
function resetHost() {
    realModalStub = makeStub();
    hostStub._child = realModalStub;      // querySelector(selector) -> realModalStub
    hostStub.querySelector = (sel) => (sel && sel.indexOf('editSaleModal') >= 0 ? realModalStub : null);
    hostStub.removeAttribute = () => { hostStub._busy = false; };
    hostStub.setAttribute = () => { hostStub._busy = true; };
    hostStub._busy = false;
}

/* bootstrap.Modal stub: instances per element, show/hide counted,
   hide() fires hidden.bs.modal like the real library. */
const instances = new Map();
function getInstance(el) { return instances.get(el); }
const bootstrapStub = {
    Modal: {
        getOrCreateInstance(el) {
            if (!instances.has(el)) {
                instances.set(el, {
                    el, shown: 0,
                    show() { this.shown++; },
                    hide() { el.fire('hidden.bs.modal'); },
                    dispose() {},
                });
            }
            return instances.get(el);
        },
        getInstance,
    },
};

/* fetch stub: scripted per call via a consume-once queue; 'hang' rejects on abort */
let fetchQueue = [];
let fetchCount = 0;
function setFetch(...steps) { fetchQueue = steps.slice(); }
function fetchStub(url, opts) {
    fetchCount++;
    const step = fetchQueue.length ? fetchQueue.shift() : { status: 200, body: '' };
    if (step === 'hang') {
        return new Promise((resolve, reject) => {
            opts.signal.addEventListener('abort', () => {
                const e = new Error('aborted'); e.name = 'AbortError'; reject(e);
            });
        });
    }
    return Promise.resolve({
        ok: step.status >= 200 && step.status < 300,
        status: step.status,
        text: () => Promise.resolve(step.body || ''),
    });
}

/* capture the loader's setTimeout so we can fire the abort timer manually */
const timers = [];
const sandbox = {
    document: documentStub,
    bootstrap: bootstrapStub,
    fetch: fetchStub,
    console: { error() {}, log() {}, debug() {} },
    AbortController: globalThis.AbortController,
    setTimeout(fn, ms) { timers.push(fn); return timers.length; },
    clearTimeout(id) { if (timers[id - 1]) timers[id - 1] = null; },
};
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(__dirname + '/../static/lazy_modal_loader.js', 'utf8'), sandbox);
const AMS = sandbox.window.AMSLazyModal;
assert.ok(AMS && typeof AMS.load === 'function', 'loader exposed window.AMSLazyModal.load');
const flush = () => new Promise(r => setTimeout(r, 10));
const fireTimers = () => { timers.forEach(fn => fn && fn()); timers.length = 0; };

(async () => {
    /* 1. success */
    resetHost();
    setFetch({ status: 200, body: 'ok' });
    AMS.load({ hostId: 'lazyEditSaleModalHost', url: '/x', selector: '[id^="editSaleModal"]', label: 'L', onReady(m) { m._ready = true; } });
    await flush();
    assert.strictEqual(fetchCount, 1, 'one fetch on success');
    assert.ok(realModalStub._ready, 'onReady called');
    assert.strictEqual(getInstance(realModalStub).shown, 1, 'real modal shown once');
    assert.strictEqual(hostStub._busy, false, 'lock freed on success');
    console.log('PASS 1 success opens modal + frees lock');

    /* 2. double-click guard */
    resetHost();
    setFetch('hang');
    const before2 = fetchCount;
    AMS.load({ hostId: 'lazyEditSaleModalHost', url: '/x', selector: '[id^="editSaleModal"]' });
    AMS.load({ hostId: 'lazyEditSaleModalHost', url: '/x', selector: '[id^="editSaleModal"]' });
    await flush();
    assert.strictEqual(fetchCount, before2 + 1, 'second click ignored while in flight');
    console.log('PASS 2 double-click fires a single fetch');

    /* 3. abort -> error dialog, lock held, Edit ignored */
    fireTimers();                       // fire the 20s abort timer
    await flush();
    assert.ok(/could not be loaded/.test(stateText()), 'error dialog rendered');
    assert.ok(/timed out/.test(stateText()), 'timeout message shown');
    const before = fetchCount;
    AMS.load({ hostId: 'lazyEditSaleModalHost', url: '/x', selector: '[id^="editSaleModal"]' });
    await flush();
    assert.strictEqual(fetchCount, before, 'Edit ignored while error dialog open (lock held)');
    console.log('PASS 3 timeout -> retry dialog, lock held');

    /* 4. Retry re-fetches and succeeds */
    setFetch({ status: 200, body: 'ok2' });
    const before4 = fetchCount;
    retryBtn.fire('click');
    await flush();
    assert.strictEqual(fetchCount, before4 + 1, 'retry fired exactly one new fetch');
    assert.strictEqual(getInstance(realModalStub).shown, 1, 'modal shown after retry');
    assert.strictEqual(hostStub._busy, false, 'lock freed after retry success');
    console.log('PASS 4 Retry re-fetches and opens the modal');

    /* 5. closing the error dialog frees the lock */
    resetHost();
    setFetch({ status: 500, body: '' });
    AMS.load({ hostId: 'lazyEditSaleModalHost', url: '/x', selector: '[id^="editSaleModal"]' });
    await flush();
    assert.ok(/Server returned 500/.test(stateText()), 'HTTP error shown');
    getInstance(shellStub).hide();      // user ESC/Cancel closes the dialog
    await flush();
    setFetch({ status: 200, body: 'ok3' });
    AMS.load({ hostId: 'lazyEditSaleModalHost', url: '/x', selector: '[id^="editSaleModal"]' });
    await flush();
    assert.strictEqual(getInstance(realModalStub).shown, 1, 'Edit works again after cancel');
    console.log('PASS 5 closing error dialog frees the lock');

    /* 6. Bootstrap missing (CDN/vendor failure) -> LOUD visible failure, no fetch,
          no silent dead Edit button */
    resetHost();
    const savedBootstrap = sandbox.bootstrap;
    let alerted = null;
    sandbox.alert = (m) => { alerted = m; };
    delete sandbox.bootstrap;
    const fetchesBefore = fetchCount;
    AMS.load({ hostId: 'lazyEditSaleModalHost', url: '/x', selector: '[id^="editSaleModal"]' });
    await flush();
    assert.strictEqual(fetchCount, fetchesBefore, 'no fetch when bootstrap is missing');
    assert.ok(alerted && /Bootstrap UI library did not load/.test(alerted), 'visible alert explains the failure');
    assert.strictEqual(hostStub._busy, false, 'no lock taken when bootstrap is missing');
    sandbox.bootstrap = savedBootstrap;
    delete sandbox.alert;
    console.log('PASS 6 missing bootstrap -> visible alert, no silent dead button');

    /* 7. A throwing onReady initializer must NOT stop the fetched form from opening */
    resetHost();
    setFetch({ status: 200, body: 'ok7' });
    let boom = null;
    AMS.load({
        hostId: 'lazyEditSaleModalHost', url: '/x', selector: '[id^="editSaleModal"]',
        onReady() { boom = new Error('init exploded'); throw boom; },
    });
    await flush();
    assert.strictEqual(getInstance(realModalStub).shown, 1, 'modal still shown when initializer throws');
    assert.strictEqual(hostStub._busy, false, 'lock freed when initializer throws');
    console.log('PASS 7 initializer failure does not block the form');

    console.log('\nALL LOADER STATE-MACHINE TESTS PASSED');
})().catch(e => { console.error('FAIL:', e && e.stack || e); process.exit(1); });
