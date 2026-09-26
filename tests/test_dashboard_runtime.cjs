const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { createRuntime } = require('../tools/dashboard-runtime.js');

function deferred() {
    let resolve, reject;
    const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
    return { promise, resolve, reject };
}
function environment(send) {
    const timers = new Map();
    let nextId = 1;
    const listeners = new Map();
    const document = {
        hidden: false,
        addEventListener: (name, fn) => listeners.set(name, fn),
        removeEventListener: name => listeners.delete(name),
    };
    const runtime = createRuntime({
        document, fetch: send,
        setTimeout(fn, delay) { const id = nextId++; timers.set(id, { fn, delay }); return id; },
        clearTimeout: id => timers.delete(id),
    });
    return { runtime, timers, document, listeners, async tick(delay) {
        const entry = [...timers].find(([, timer]) => timer.delay === delay);
        assert.ok(entry, `expected timer at ${delay} ms`);
        timers.delete(entry[0]);
        return entry[1].fn();
    } };
}
function response(body) { return { ok: true, status: 200, json: async () => body }; }
function abortable(_url, { signal }) {
    return new Promise((_, reject) => {
        if (signal.aborted) return reject(Object.assign(new Error(), { name: 'AbortError' }));
        signal.addEventListener('abort', () => reject(Object.assign(new Error(), { name: 'AbortError' })), { once: true });
    });
}

test('repeated refresh calls share a pending GET and clean up afterwards', async () => {
    const body = deferred();
    let calls = 0;
    const { runtime, timers } = environment(async () => { calls++; return { ok: true, status: 200, json: () => body.promise }; });
    const a = runtime.request('/status');
    const b = runtime.request('/status');
    const c = runtime.request('/status');
    assert.equal(a, b); assert.equal(b, c); assert.equal(calls, 1);
    body.resolve({ state: 'ok' });
    assert.deepEqual(await (await a).json(), { state: 'ok' });
    assert.equal(timers.size, 0);
    await runtime.request('/status');
    assert.equal(calls, 2);
    runtime.dispose();
});

test('navigation aborts requests and rejects late JSON even if transport ignores abort', async () => {
    const body = deferred();
    const { runtime } = environment(async () => ({ ok: true, status: 200, json: () => body.promise }));
    const request = runtime.request('/status');
    await Promise.resolve();
    const rejected = assert.rejects(request, { name: 'AbortError' });
    runtime.beginPage();
    body.resolve({ stale: true });
    await rejected;
    runtime.dispose();
});

test('request timeout rejects visibly and clears the in-flight slot', async () => {
    const env = environment(abortable);
    const request = env.runtime.request('/status', {}, { timeout: 25 });
    const rejected = assert.rejects(request, { name: 'TimeoutError' });
    await env.tick(25);
    await rejected;
    assert.equal(env.timers.size, 0);
    env.runtime.dispose();
});

test('polls wait for completion, pause while hidden and resume once', async () => {
    const env = environment(abortable);
    const work = deferred();
    let calls = 0;
    env.runtime.startPoll(() => { calls++; return work.promise; }, 5000);
    const firstTick = env.tick(5000);
    assert.equal(calls, 1);
    assert.equal(env.timers.size, 0);
    env.document.hidden = true;
    env.listeners.get('visibilitychange')();
    work.resolve(); await firstTick;
    assert.equal(env.timers.size, 0);
    env.document.hidden = false;
    env.listeners.get('visibilitychange')();
    assert.equal(env.timers.size, 1);
    await env.tick(0);
    assert.equal(calls, 2);
    env.runtime.dispose();
    assert.equal(env.timers.size, 0);
});

test('page changes stop page polling but keep one app-wide poll', () => {
    const env = environment(abortable);
    env.runtime.startPoll(async () => {}, 2000);
    env.runtime.startPoll(async () => {}, 5000, { global: true });
    env.runtime.beginPage();
    assert.deepEqual([...env.timers.values()].map(x => x.delay), [5000]);
    env.runtime.dispose();
});

test('mutations are never deduplicated or automatically retried', async () => {
    let calls = 0;
    const env = environment(async () => { calls++; return response({ ok: true }); });
    await Promise.all([env.runtime.request('/settings', { method: 'POST' }), env.runtime.request('/settings', { method: 'POST' })]);
    assert.equal(calls, 2);
    env.runtime.dispose();
});

test('a timed-out mutation reports an unknown outcome and sends only once', async () => {
    let calls = 0;
    const env = environment((url, options) => { calls++; return abortable(url, options); });
    const request = env.runtime.request('/settings', { method: 'POST' });
    const rejected = assert.rejects(request, error => error.name === 'TimeoutError' && error.message.includes('结果尚未确认'));
    await env.tick(30000);
    await rejected;
    assert.equal(calls, 1);
    assert.equal(env.timers.size, 0);
    env.runtime.dispose();
});

test('the app status badge and a page share one app-wide pending read across navigation', async () => {
    const body = deferred();
    let calls = 0;
    const env = environment(async () => { calls++; return { ok: true, status: 200, json: () => body.promise }; });
    const badge = env.runtime.request('/metrics', {}, { global: true });
    env.runtime.beginPage();
    const page = env.runtime.request('/metrics', {}, { global: true });
    assert.equal(badge, page);
    body.resolve({ pending: 0 });
    await badge;
    assert.equal(calls, 1);
    env.runtime.dispose();
});

const html = fs.readFileSync(path.join(__dirname, '../tools/dashboard.html'), 'utf8');
const appSource = fs.readFileSync(path.join(__dirname, '../tools/dashboard-app.js'), 'utf8');
const sectionSource = appSource.slice(appSource.indexOf('async function loadSection('), appSource.indexOf('async function loadPersons('));
function sectionContext(env) {
    const elements = {};
    let screen = '';
    const context = vm.createContext({
        API: '', dashboardRuntime: env.runtime, dashboardRequest: env.runtime.request,
        isCancelledRequest: error => error?.name === 'AbortError',
        document: { getElementById: id => elements[id] || null },
        render(value) {
            screen = value;
            for (const key of Object.keys(elements)) delete elements[key];
            for (const match of value.matchAll(/id="([^"]+)"/g)) elements[match[1]] = { innerHTML: 'LOADING' };
        },
        skeleton: () => 'LOADING', pageHead: () => 'OVERVIEW', overviewCover: () => 'SUMMARY',
        fmt: String, renderBars: () => 'CHART', personTable: () => 'RECENT',
        emptyState: title => `ERROR:${title}`, esc: String,
    });
    vm.runInContext(sectionSource, context);
    return { context, elements, screen: () => screen };
}

test('overview renders successful sections before a slow optional request finishes', async () => {
    const slow = deferred();
    const env = environment(async url => url.endsWith('/recent') ? slow.promise : response(url.endsWith('/stats') ? {} : []));
    const { context, elements } = sectionContext(env);
    const loading = context.loadOverview();
    for (let i = 0; i < 15; i++) await Promise.resolve();
    assert.match(elements.overviewSummary.innerHTML, /SUMMARY/);
    assert.equal(elements.cityChart.innerHTML, 'CHART');
    assert.equal(elements.overviewRecent.innerHTML, 'LOADING');
    slow.reject(new Error('simulated optional failure'));
    await loading;
    assert.match(elements.overviewSummary.innerHTML, /SUMMARY/);
    assert.match(elements.overviewRecent.innerHTML, /^ERROR:/);
    env.runtime.dispose();
});

test('late overview responses cannot replace a newer page', async () => {
    const slow = deferred();
    const env = environment(async () => slow.promise);
    const { context, screen } = sectionContext(env);
    const loading = context.loadOverview();
    env.runtime.beginPage();
    context.render('NEW_PAGE');
    slow.resolve(response([]));
    await loading;
    assert.equal(screen(), 'NEW_PAGE');
    env.runtime.dispose();
});

test('dashboard source parses and fonts do not depend on external stylesheets', () => {
    new vm.Script(appSource);
    assert.doesNotMatch(html, /<link[^>]+https?:\/\//);
    assert.doesNotMatch(appSource, /\bsetInterval\(/);
    assert.doesNotMatch(html, /<style>|<script>/);
    assert.match(html, /<link rel="stylesheet" href="\/assets\/dashboard\.css">/);
    const scripts = [...html.matchAll(/<script src="([^"]+)"/g)].map(match => match[1]);
    assert.deepEqual(scripts, ['/assets/dashboard-runtime.js', '/assets/dashboard-app.js']);
});
