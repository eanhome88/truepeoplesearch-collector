const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { createRuntime } = require('../tools/dashboard-runtime.js');

const source = fs.readFileSync(path.join(__dirname, '../tools/dashboard-app.js'), 'utf8');
function section(start, end) {
    const from = source.indexOf(start);
    const to = source.indexOf(end, from + start.length);
    assert.ok(from >= 0 && to > from, `missing ${start}`);
    return source.slice(from, to);
}
function deferred() {
    let resolve;
    const promise = new Promise(yes => { resolve = yes; });
    return { promise, resolve };
}
const response = (body, ok = true) => ({ ok, status: ok ? 200 : 503, json: async () => body });

test('initial cluster logs remain literal text in rendered markup', () => {
    let html = '';
    const ctx = vm.createContext({
        esc: value => String(value ?? '').replace(/[&<>"']/g, c => ({
            '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
        }[c])),
        fmt: n => String(n || 0),
        pageHead: () => '',
        render: value => { html = value; },
        bindProxyClusterEvents() {},
    });
    vm.runInContext(section('function renderProxyClusterView(', 'function bindProxyClusterEvents('), ctx);
    ctx.renderProxyClusterView({ mode: 'direct' }, {
        running: false, logs: ['<img src=x onerror=alert(1)>', '& local status'],
    }, null);
    assert.match(html, /&lt;img src=x onerror=alert\(1\)&gt;/);
    assert.match(html, /&amp; local status/);
    assert.doesNotMatch(html, /<img src=x/);
    assert.match(html, /id="liveCfRate">—</);
    assert.match(html, /指标暂不可用/);
});

for (const broken of ['config', 'status']) {
    test(`${broken} read failure displays an error instead of a fabricated management state`, async () => {
        const renders = [];
        let normal = 0;
        let polls = 0;
        const ctx = vm.createContext({
            API: '',
            dashboardRuntime: { currentPage: () => 1, isCurrent: () => true,
                startPoll() { polls++; } },
            dashboardRequest: url => {
                if (url.endsWith('/config')) return Promise.resolve(response({ error: 'unavailable' }, broken !== 'config'));
                if (url.endsWith('/status')) return Promise.resolve(response({ error: 'unavailable' }, broken !== 'status'));
                return Promise.resolve(response({ counters: {} }));
            },
            stopPipelinePoll() {}, stopProxyPoll() {},
            skeleton: () => 'loading',
            render: value => renders.push(value),
            emptyState: (title, detail) => `${title}: ${detail}`,
            renderProxyClusterView() { normal++; },
            isCancelledRequest: () => false,
        });
        vm.runInContext(section('async function loadProxyCluster()', 'function renderProxyClusterView('), ctx);
        await ctx.loadProxyCluster();
        assert.equal(normal, 0);
        assert.equal(polls, 0);
        assert.match(renders.at(-1), /加载失败/);
        assert.doesNotMatch(renders.at(-1), /集群已停止|直连/);
    });
}

test('a late scale preview cannot replace a newer filter or report its stale error', async () => {
    const pending = [];
    const shown = [];
    const error = { hidden: true, textContent: '' };
    let letter = 'a';
    const ctx = vm.createContext({
        API: '', URLSearchParams, scalePreviewSeq: 0,
        dashboardRuntime: { currentPage: () => 1, isCurrent: () => true },
        currentSliceFields: () => ({ letters: letter }),
        dashboardRequest: url => { const work = deferred(); pending.push({ url, work }); return work.promise; },
        renderScale: value => shown.push(value.id),
        isCancelledRequest: () => false,
        document: { getElementById: () => error },
    });
    vm.runInContext(section('async function previewScale()', 'async function saveSlice()'), ctx);
    const old = ctx.previewScale();
    letter = 'b';
    const latest = ctx.previewScale();
    pending[1].work.resolve(response({ id: 'b' }));
    await latest;
    pending[0].work.resolve(response({ error: 'old failure' }, false));
    await old;
    assert.deepEqual(shown, ['b']);
    assert.equal(error.hidden, true);
    assert.match(pending[0].url, /letters=a/);
    assert.match(pending[1].url, /letters=b/);
});

test('a late directory response cannot overwrite the current filter or its error', async () => {
    const pending = [];
    const shown = [];
    const box = { innerHTML: '' };
    let kind = 'letter';
    const ctx = vm.createContext({
        API: '', URLSearchParams, pipelineReady: true, dirLoadSeq: 0,
        dashboardRuntime: { currentPage: () => 1, isCurrent: () => true },
        dirParams: () => ({ kind }),
        dashboardRequest: url => { const work = deferred(); pending.push({ url, work }); return work.promise; },
        renderDirTable: value => shown.push(value.id),
        isCancelledRequest: () => false,
        document: { getElementById: () => box },
        esc: value => String(value),
    });
    vm.runInContext(section('async function loadDirs()', 'async function postDirs('), ctx);
    const old = ctx.loadDirs();
    kind = 'surname';
    const latest = ctx.loadDirs();
    pending[1].work.resolve(response({ id: 'surname' }));
    await latest;
    pending[0].work.resolve(response({ error: 'old failure' }, false));
    await old;
    assert.deepEqual(shown, ['surname']);
    assert.equal(box.innerHTML, '');
    assert.match(pending[0].url, /kind=letter/);
    assert.match(pending[1].url, /kind=surname/);
});

test('failed status refresh marks controls unavailable and successful refresh restores plain numbers', async () => {
    const elements = new Map(['clusterLiveDot', 'clusterLiveStatus', 'btnToggleCluster',
        'liveQps', 'liveBuffer', 'liveCfRate'].map(id => [id, { textContent: '', className: '',
            disabled: false, style: {} }]));
    let status = response({ ok: false }, false);
    const ctx = vm.createContext({
        API: '', clusterBusy: false, clusterStatusSeq: 0,
        dashboardRuntime: { currentPage: () => 1, isCurrent: () => true },
        dashboardRequest: url => Promise.resolve(url.endsWith('/status') ? status
            : response({ counters: { success: 5, cf_fail: 1 } })),
        document: { getElementById: id => elements.get(id) || null },
        setText: (id, value) => { const el = elements.get(id); if (el) el.textContent = String(value); },
        fmt: n => String(n || 0),
        isCancelledRequest: () => false,
    });
    vm.runInContext(section('function showClusterStatusUnavailable()', 'function route()'), ctx);
    assert.equal(await ctx.refreshClusterLiveStatus(), false);
    assert.equal(elements.get('clusterLiveStatus').textContent, '状态暂不可用');
    assert.equal(elements.get('btnToggleCluster').disabled, true);
    status = response({ ok: true, running: false, total_qps: 1.5, buffer_depth: 8 });
    assert.equal(await ctx.refreshClusterLiveStatus(), true);
    assert.equal(elements.get('clusterLiveStatus').textContent, '集群已停止');
    assert.equal(elements.get('btnToggleCluster').disabled, false);
    assert.equal(elements.get('liveQps').textContent, '1.5');
    assert.equal(elements.get('liveBuffer').textContent, '8');
    assert.doesNotMatch(elements.get('liveCfRate').textContent, /<span/);
});

test('a control action reads fresh status and a late pre-action poll cannot overwrite it', async () => {
    const statusRequests = [];
    let clickCluster;
    const elements = new Map();
    const dot = { className: 'live-dot',
        classList: { contains: name => dot.className.split(/\s+/).includes(name) } };
    const button = { disabled: false, textContent: '一键启动', style: {},
        addEventListener: (name, callback) => { if (name === 'click') clickCluster = callback; } };
    elements.set('clusterLiveDot', dot);
    elements.set('clusterLiveStatus', { textContent: '集群已停止' });
    elements.set('btnToggleCluster', button);
    const document = {
        hidden: false,
        addEventListener() {}, removeEventListener() {},
        querySelectorAll: () => [],
        getElementById: id => elements.get(id) || null,
    };
    const runtime = createRuntime({
        document,
        fetch: (url, init) => {
            if (url.endsWith('/cluster/status')) {
                const work = deferred();
                statusRequests.push({ work, init });
                return work.promise;
            }
            if (url.endsWith('/cluster/control')) return Promise.resolve(response({ ok: true }));
            if (url.endsWith('/metrics')) return Promise.resolve(response({ counters: {} }));
            throw new Error(`unexpected request: ${url}`);
        },
        setTimeout: () => 1, clearTimeout() {},
    });
    const ctx = vm.createContext({
        API: '', document, dashboardRuntime: runtime,
        dashboardRequest: runtime.request,
        clusterBusy: false, clusterStatusSeq: 0, proxyTesting: false,
        isCancelledRequest: error => error?.name === 'AbortError',
        setText: (id, value) => { const el = elements.get(id); if (el) el.textContent = String(value); },
        fmt: value => String(value || 0),
        alert: message => { throw new Error(message); },
    });
    vm.runInContext(section('function bindProxyClusterEvents(', 'function showClusterStatusUnavailable()'), ctx);
    vm.runInContext(section('function showClusterStatusUnavailable()', 'function route()'), ctx);
    try {
        ctx.bindProxyClusterEvents({ mode: 'direct' }, { running: false });
        assert.equal(typeof clickCluster, 'function');
        const oldPoll = ctx.refreshClusterLiveStatus();
        assert.equal(statusRequests.length, 1);

        const action = clickCluster();
        for (let i = 0; i < 20 && statusRequests.length < 2; i++) await Promise.resolve();
        assert.equal(statusRequests.length, 2, 'post-action status must not share the old pending GET');
        assert.equal(statusRequests[1].init.cache, 'no-store');
        statusRequests[1].work.resolve(response({ ok: true, running: true }));
        await action;
        assert.equal(elements.get('clusterLiveStatus').textContent, '集群运行中');

        statusRequests[0].work.resolve(response({ ok: true, running: false }));
        await oldPoll;
        assert.equal(elements.get('clusterLiveStatus').textContent, '集群运行中');
        assert.equal(dot.classList.contains('on'), true);
        assert.equal(button.textContent, '停止抓取集群');
    } finally {
        runtime.dispose();
    }
});

test('keyboard activates link cards once and leaves native buttons to their click event', () => {
    let listener;
    const visits = [];
    const ctx = vm.createContext({
        document: { getElementById: () => ({ addEventListener: (_name, fn) => { listener = fn; } }) },
        go: path => visits.push(path),
    });
    vm.runInContext(section("document.getElementById('mainContent').addEventListener('keydown'", 'const searchBox ='), ctx);
    const target = role => ({ dataset: { go: '/pipeline' }, getAttribute: () => role,
        closest: selector => selector === '[data-go]' ? targetNode : null });
    let targetNode = target('link');
    let prevented = 0;
    listener({ key: 'Enter', target: targetNode, preventDefault: () => prevented++ });
    listener({ key: ' ', target: targetNode, preventDefault: () => prevented++ });
    targetNode = target(null);
    listener({ key: 'Enter', target: targetNode, preventDefault: () => prevented++ });
    assert.deepEqual(visits, ['/pipeline', '/pipeline']);
    assert.equal(prevented, 2);
});
