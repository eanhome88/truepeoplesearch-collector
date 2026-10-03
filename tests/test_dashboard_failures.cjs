const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

// Run only the selected UI functions with synthetic responses and DOM nodes.
// No real browser, local API, export data, timer, or network is used.
const source = fs.readFileSync(path.join(__dirname, '../tools/dashboard-app.js'), 'utf8');
function section(start, end) {
    const from = source.indexOf(start);
    const to = source.indexOf(end, from + start.length);
    assert.ok(from >= 0 && to > from, `missing ${start}`);
    return source.slice(from, to);
}

function response(body, status = 200, contentType = 'application/json') {
    return {
        ok: status >= 200 && status < 300, status,
        headers: { get: () => contentType },
        json: async () => body,
        blob: async () => ({ synthetic: true }),
    };
}

function environment(reply) {
    const nodes = {
        personsTable: { innerHTML: '' },
        personsStatsBar: { innerHTML: 'stale success stats' },
        personsExportStatus: { textContent: '' },
        btnExportCsv: { disabled: false, innerHTML: '' },
        globalSearch: { value: '' },
    };
    nodes.mainContent = { querySelector: () => nodes.personsTable };
    const renders = [];
    const timers = new Map();
    const downloads = [];
    const revoked = [];
    let timerId = 0;
    let current = true;
    let sends = 0;
    const ctx = vm.createContext({
        API: '', URLSearchParams, AbortController,
        URL: { createObjectURL: () => 'blob:synthetic', revokeObjectURL: url => revoked.push(url) },
        personQuery: '', phoneQuery: '', cityFilter: '', stateFilter: '',
        phoneTypeFilter: 'all', hasWirelessFilter: false, ageMinFilter: '', ageMaxFilter: '', sortFilter: 'newest',
        currentPage: 1, totalPages: 1, ICONS: { search: '' },
        dashboardRuntime: { currentPage: () => 1, isCurrent: () => current },
        dashboardRequest: async () => reply,
        fetch: async () => { sends++; return reply; },
        setTimeout: (fn, delay) => { const id = ++timerId; timers.set(id, { fn, delay }); return id; },
        clearTimeout: id => timers.delete(id),
        isCancelledRequest: error => error.name === 'AbortError',
        document: {
            getElementById: id => nodes[id] || null,
            createElement: () => ({ click() { downloads.push(this.href); } }),
            body: { appendChild() {}, removeChild() {} },
        },
        emptyState: (title, detail) => `${title}: ${detail}`,
        pageHead: () => '', render: html => renders.push(html),
        esc: value => String(value), fmt: value => String(value),
        personTable: () => 'synthetic table',
    });
    vm.runInContext(section('async function readDashboardDataResponse(', 'function copyPhone('), ctx);
    vm.runInContext(section('async function loadPersons(', 'function clearCity('), ctx);
    vm.runInContext(section('async function loadSearchPage(', 'async function loadCharts('), ctx);
    return { ctx, nodes, renders, timers, downloads, revoked,
        sends: () => sends, navigate: () => { current = false; } };
}

for (const [label, reply] of [
    ['HTTP 500', response({ error: 'private synthetic exception' }, 500)],
    ['HTTP 200 error payload', response({ error: 'private synthetic exception' })],
    ['malformed payload', response({})],
]) {
    test(`list ${label} shows failure, not no matching records`, async () => {
        const env = environment(reply);
        await env.ctx.loadPersons();
        assert.match(env.nodes.personsTable.innerHTML, /加载失败/);
        assert.doesNotMatch(env.nodes.personsTable.innerHTML, /未匹配到|private synthetic/);
        assert.equal(env.nodes.personsStatsBar.innerHTML, '');
    });
    test(`search ${label} shows failure, not no results`, async () => {
        const env = environment(reply);
        await env.ctx.loadSearchPage('synthetic');
        assert.match(env.renders.at(-1), /搜索失败/);
        assert.doesNotMatch(env.renders.at(-1), /无匹配结果|private synthetic/);
    });
}

test('valid empty list/search responses keep their ordinary empty states', async () => {
    const list = environment(response({ data: [], total: 0, with_wireless: 0, page: 1, size: 20 }));
    await list.ctx.loadPersons();
    assert.match(list.nodes.personsTable.innerHTML, /未匹配到符合条件的数据/);
    const search = environment(response({ persons: [], phones: [], emails: [] }));
    await search.ctx.loadSearchPage('synthetic');
    assert.match(search.renders.at(-1), /无匹配结果/);
});

test('HTTP 400 validation detail is visible to the list and export user', async () => {
    const env = environment(response({ error: '最小年龄不能大于最大年龄' }, 400));
    await env.ctx.loadPersons();
    assert.match(env.nodes.personsTable.innerHTML, /最小年龄不能大于最大年龄/);
    await env.ctx.exportPersonsCsv();
    assert.match(env.nodes.personsExportStatus.textContent, /导出失败.*最小年龄不能大于最大年龄/);
    assert.equal(env.nodes.btnExportCsv.disabled, false);
    assert.deepEqual(env.downloads, []);
    assert.equal(env.timers.size, 0);
});

test('export HTTP errors are visible and never saved as a download', async () => {
    const env = environment(response({ error: 'password=DO_NOT_LEAK' }, 500));
    await env.ctx.exportPersonsCsv();
    assert.match(env.nodes.personsExportStatus.textContent, /导出失败/);
    assert.doesNotMatch(env.nodes.personsExportStatus.textContent, /DO_NOT_LEAK/);
    assert.equal(env.nodes.btnExportCsv.disabled, false);
    assert.deepEqual(env.downloads, []);
    assert.equal(env.timers.size, 0);
});

test('export rejects an unexpected success response type instead of downloading JSON', async () => {
    const env = environment(response({ error: 'synthetic JSON response' }));
    await env.ctx.exportPersonsCsv();
    assert.match(env.nodes.personsExportStatus.textContent, /导出失败.*未返回导出文件/);
    assert.deepEqual(env.downloads, []);
});

test('successful export restores the button and releases its temporary URL', async () => {
    const env = environment(response(null, 200, 'text/csv; charset=utf-8-sig'));
    await env.ctx.exportPersonsCsv();
    assert.deepEqual(env.downloads, ['blob:synthetic']);
    assert.equal(env.nodes.btnExportCsv.disabled, false);
    assert.match(env.nodes.personsExportStatus.textContent, /已准备好/);
    assert.equal(env.timers.size, 1);
    [...env.timers.values()][0].fn();
    assert.deepEqual(env.revoked, ['blob:synthetic']);
});

test('duplicate export clicks share no extra request, and navigation suppresses stale download', async () => {
    const env = environment(response(null, 200, 'text/csv'));
    let resolve;
    let calls = 0;
    env.ctx.fetch = () => { calls++; return new Promise(yes => { resolve = yes; }); };
    const pending = env.ctx.exportPersonsCsv();
    await env.ctx.exportPersonsCsv();
    assert.equal(calls, 1);
    env.navigate();
    resolve(response(null, 200, 'text/csv'));
    await pending;
    assert.deepEqual(env.downloads, []);
    assert.equal(env.nodes.btnExportCsv.disabled, false);
    assert.equal(env.timers.size, 0);
});

test('export timeout is visible and releases the busy state', async () => {
    const env = environment(response(null, 200, 'text/csv'));
    env.ctx.fetch = (_, { signal }) => new Promise((resolve, reject) => {
        signal.addEventListener('abort', () => reject(Object.assign(new Error('synthetic abort'), { name: 'AbortError' })));
    });
    const pending = env.ctx.exportPersonsCsv();
    [...env.timers.values()][0].fn();
    await pending;
    assert.match(env.nodes.personsExportStatus.textContent, /导出失败.*超时/);
    assert.equal(env.nodes.btnExportCsv.disabled, false);
    assert.equal(env.timers.size, 0);
});
