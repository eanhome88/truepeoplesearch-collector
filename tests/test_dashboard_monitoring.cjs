const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../tools/dashboard-app.js'), 'utf8');
function section(start, end) {
    const from = source.indexOf(start);
    const to = source.indexOf(end, from + start.length);
    assert.ok(from >= 0 && to > from);
    return source.slice(from, to);
}

async function overview(data) {
    let html;
    const context = vm.createContext({
        API: '', render() {}, pageHead: () => '', skeleton: () => '',
        overviewCover: () => 'coverage',
        esc: value => String(value ?? '').replace(/[<>]/g, ''),
        loadSection: async (id, url, renderContent) => {
            if (id === 'overviewSummary') html = renderContent(data);
        },
    });
    vm.runInContext(section('function metricValue(', 'async function loadPersons('), context);
    await context.loadOverview();
    return { html, context };
}

const empty = {
    metrics_available: true, throughput_available: true, database_available: true,
    metrics_quality: { status: 'no_samples' }, total_tasks_executed: 0, success_tasks: 0,
    current_qps: 0, avg_latency_ms: null, success_rate_pct: null, dedup_saved_count: 0,
    persons: 87, phones: 0, emails: 0, prev_addr: 0, wireless_count: 0,
    primary_wireless_count: 22, smart_fallback_count: 0, wireless_ratio_pct: 0,
};

test('stopped overview preserves zero QPS and does not invent successful jobs', async () => {
    const { html } = await overview(empty);
    assert.match(html, /0\.0 <span[^>]*>QPS/);
    assert.match(html, /上报成功 <span[^>]*>0<\/span>/);
    assert.match(html, /暂无任务样本/);
    assert.doesNotMatch(html, /32\.0|99\.8|96\.8|28x|零丢单/);
    assert.match(html, /不等同于新增入库数/);
    assert.match(html, /未接入实际计量/);
});

test('real zero success rate and zero measured latency remain zero', async () => {
    const { html } = await overview({ ...empty, total_tasks_executed: 5,
        metrics_quality: { status: 'valid' }, success_rate_pct: 0, avg_latency_ms: 0 });
    assert.match(html, /0\.00<span[^>]*>%/);
    assert.match(html, /0\.0 ms/);
});

test('unavailable statistics do not become zero or fallback to stored persons', async () => {
    const { html } = await overview({ ...empty, metrics_available: false,
        throughput_available: false, database_available: false,
        metrics_quality: { status: 'unavailable' } });
    assert.match(html, /指标暂不可用/);
    assert.match(html, /数据库统计暂不可用/);
    assert.match(html, /实时吞吐暂不可用/);
    assert.match(html, /— <span[^>]*>QPS/);
    assert.doesNotMatch(html, /coverage|>87</);
});

test('inconsistent or demo metrics show warnings without a success percentage', async () => {
    for (const status of ['inconsistent', 'demo']) {
        const { html } = await overview({ ...empty, metrics_quality: { status },
            success_rate_pct: 151.95, avg_latency_ms: 43, success_tasks: 8542 });
        assert.doesNotMatch(html, /151\.95/);
        assert.match(html, /演示数据/);
    }
});

test('a new frontend does not trust an old backend without quality flags', async () => {
    const { html } = await overview({ persons: 8, success_tasks: 8542,
        total_tasks_executed: 8560, current_qps: 32, success_rate_pct: 99.79,
        traffic_saved_gb: 21.8, avg_latency_ms: 43 });
    assert.match(html, /来源未确认/);
    assert.doesNotMatch(html, /32\.0|99\.79|21\.8|8,542/);
});

test('metric formatter accepts finite nonnegative numbers only', async () => {
    const { context } = await overview(empty);
    for (const value of [null, undefined, '', '32', NaN, Infinity, -1, true]) {
        assert.equal(context.metricValue(value, 1), '—');
    }
    assert.equal(context.metricValue(0, 1), '0.0');
    assert.equal(context.metricValue(12.34, 1), '12.3');
});

test('cluster refresh hides contaminated rates and uses all attempts as denominator', async () => {
    const values = {};
    let metrics = { metrics_quality: { status: 'inconsistent' },
        counters: { attempt: 10, success: 8, cf_fail: 1 } };
    const context = vm.createContext({
        API: '', clusterStatusSeq: 0, clusterBusy: false,
        dashboardRuntime: { currentPage: () => 1, isCurrent: () => true },
        dashboardRequest: async url => ({ ok: true, json: async () => url.endsWith('/status')
            ? { ok: true, running: false, total_qps: 0 } : metrics }),
        setText: (key, value) => { values[key] = value; },
        fmt: value => String(value),
        metricValue: (value, decimals) => typeof value === 'number' ? value.toFixed(decimals) : '—',
        isCancelledRequest: () => false,
        document: { getElementById: () => null },
    });
    vm.runInContext(section('function showClusterStatusUnavailable()', 'function route()'), context);
    assert.equal(await context.refreshClusterLiveStatus(), true);
    assert.equal(values.liveCfRate, '—');
    metrics = { ...metrics, metrics_quality: { status: 'valid' } };
    assert.equal(await context.refreshClusterLiveStatus(), true);
    assert.equal(values.liveCfRate, '10.0%');
});

function pipeline(data) {
    const values = {};
    const context = vm.createContext({
        pipelineBusy: false, scaleDirty: false, lastScale: null, window: {},
        document: { getElementById: () => null },
        hydrateSlice() {}, renderScale() {}, renderPlan() {}, setScaleLocked() {},
        setSwitch() {}, setDisabled() {},
        setText: (id, value) => { values[id] = value; },
        fmt: n => String(n), fmtYi: n => String(n), fmtPct: n => n, fmtShare: () => '0%',
        formatEta: n => `${n} seconds`,
    });
    vm.runInContext(section('function metricValue(', 'function metricsQualityNotice('), context);
    vm.runInContext(section('function applyPipeline(', 'async function refreshPipeline('), context);
    context.applyPipeline(data);
    return values;
}

test('stopped pipeline cannot turn stale leases into active work or inventory into successes', () => {
    const values = pipeline({ persons: 87, queue: { pending: 100, processing: 266 },
        worker: { running: false, concurrency: 2 }, metrics: { metrics_available: true,
            metrics_quality: { status: 'valid' }, counters: { success: 0 },
            latency: { scrape_ms: { avg: 1000 } } } });
    assert.equal(values.flowSuccessVal, '0');
    assert.equal(values.flowInflightVal, '0');
    assert.equal(values.jobCount, '0');
    assert.equal(values.flowRateVal, '确认入库 0.0 QPS · 非新增人数');
    assert.match(values.pipeEta, /不可估算/);
});

test('pipeline with invalid or unavailable samples displays unknown success and no fabricated ETA', () => {
    for (const quality of ['unavailable', 'inconsistent', 'no_samples']) {
        const values = pipeline({ persons: 87, queue: { pending: 100 },
            worker: { running: true, concurrency: 2 }, metrics: {
                metrics_available: false, metrics_quality: { status: quality },
                counters: { success: null }, latency: { scrape_ms: { avg: null } } } });
        assert.equal(values.flowSuccessVal, '—');
        assert.match(values.pipeEta, /不可估算/);
        assert.equal(values.flowRateVal, '入库速率暂不可用');
    }
});

test('ETA is explicitly a historical estimate and suppressed while paused', () => {
    const data = { queue: { pending: 120 }, worker: { running: true, concurrency: 2 },
        metrics: { metrics_available: true, metrics_quality: { status: 'valid' },
            counters: { success: 1 }, latency: { scrape_ms: { avg: 1000 } } } };
    assert.match(pipeline(data).pipeEta, /按历史耗时粗估 60 seconds/);
    assert.match(pipeline({ ...data, worker: { ...data.worker, paused: true } }).pipeEta, /不可估算/);
});
