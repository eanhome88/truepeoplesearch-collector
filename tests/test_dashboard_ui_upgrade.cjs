const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.join(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'tools/dashboard.html'), 'utf8');
const css = fs.readFileSync(path.join(root, 'tools/dashboard.css'), 'utf8');
const app = fs.readFileSync(path.join(root, 'tools/dashboard-app.js'), 'utf8');

function sourceSection(start, end) {
    const from = app.indexOf(start);
    const to = app.indexOf(end, from + start.length);
    assert.ok(from >= 0 && to > from, `missing source section: ${start}`);
    return app.slice(from, to);
}

test('dashboard shell uses only local visual assets and system font fallbacks', () => {
    const assets = [...html.matchAll(/\b(?:src|href)="([^"]+)"/g)].map(match => match[1]);
    assert.deepEqual(assets, [
        '/assets/dashboard.css',
        '/assets/dashboard-runtime.js',
        '/assets/dashboard-app.js',
    ]);
    assert.doesNotMatch(html, /\b(?:src|href)="(?:https?:)?\/\//i);
    assert.doesNotMatch(css, /@import\b/i);
    assert.doesNotMatch(css, /@font-face\b/i);
    assert.doesNotMatch(css, /url\(\s*["']?(?:https?:)?\/\//i);
    assert.match(css, /--font-ui:[^;]*system-ui[^;]*sans-serif/i);
});

test('keyboard focus remains visible after the base focus reset', () => {
    assert.match(css, /:focus\s*\{\s*outline:\s*none;\s*\}/);
    const focusVisible = css.match(/:focus-visible\s*\{([\s\S]*?)\}/);
    assert.ok(focusVisible, 'a focus-visible rule is required');
    assert.match(focusVisible[1], /outline:\s*2px\s+solid\s+var\(--accent\)/);
    assert.match(focusVisible[1], /outline-offset:\s*2px/);
});

test('shell controls use native semantics and the update panel is labelled', () => {
    assert.match(html, /<button type="button" class="version-pill" id="versionBadge"/);
    assert.match(html, /class="topbar-context" aria-label="本机控制台"/);
    assert.match(html, /id="updateModal" role="dialog" aria-modal="true" aria-labelledby="updateModalTitle"/);
    assert.match(html, /<h3 class="modal-title" id="updateModalTitle">/);
    assert.match(html, /id="modalCloseBtn" aria-label="关闭版本详情"/);
});

test('update dialog preserves keyboard containment and focus return', () => {
    assert.match(app, /let modalFocusOrigin = null;/);
    assert.match(app, /function getModalFocusableControls\(\)/);
    assert.match(app, /function restoreFocusAfterClose\(\)/);
    assert.match(app, /event\.key === 'Escape'/);
    assert.match(app, /event\.key !== 'Tab'/);
    assert.match(app, /focusControl\(shouldWrapBackward \? lastControl : firstControl\)/);
});

test('customer release mode hides control routes and never starts online update polling', () => {
    assert.match(app, /currentSystemInfo\.release_mode === 'customer'/);
    assert.match(app, /function applyCustomerReleasePresentation\(\)/);
    assert.match(app, /\[data-route="\/pipeline"\], \[data-route="\/proxy"\]/);
    assert.match(app, /loadVersionInfo\(\)\.then\(\(\) => \{[\s\S]*?if \(customerReleaseMode\) return;/);
    assert.match(app, /async function checkUpdate\(interactive = false\) \{\s*if \(customerReleaseMode\) return;/);
});

test('reduced-motion preference disables both animation and transition', () => {
    const motion = css.match(/@media\s*\(prefers-reduced-motion:\s*reduce\)\s*\{([\s\S]*?)\n\s*\}/);
    assert.ok(motion, 'a prefers-reduced-motion media query is required');
    assert.match(motion[1], /animation:\s*none\s*!important/);
    assert.match(motion[1], /transition:\s*none\s*!important/);
});

test('responsive layout keeps both tablet and mobile adaptation rules', () => {
    assert.match(css, /@media\s*\(max-width:\s*1040px\)/);
    assert.match(css, /@media\s*\(max-width:\s*860px\)/);
    const mobile = css.slice(css.lastIndexOf('@media (max-width: 860px)'));
    assert.match(mobile, /\.sidebar\s*\{[\s\S]*?position:\s*fixed/);
    assert.match(mobile, /\.menu-btn\s*\{\s*display:\s*grid/);
    assert.match(mobile, /\.bento\s*\{\s*grid-template-columns:\s*1fr/);
});

test('metric helpers keep unavailable states explicit rather than inventing numbers', () => {
    const context = vm.createContext({ esc: value => String(value ?? '') });
    vm.runInContext(sourceSection('function metricValue(', 'async function loadOverview()'), context);

    assert.equal(context.metricValue(0), '0');
    assert.equal(context.metricValue(null), '—');
    assert.equal(context.metricValue(-1), '—');

    const notice = context.metricsQualityNotice({
        metrics_quality: { status: 'inconsistent' },
        database_available: true,
        throughput_available: true,
    });
    assert.match(notice, /成功率暂不可用/);
    assert.doesNotMatch(notice, /\b(?:32(?:\.0)?|99\.8)\b/);

    const unavailable = context.metricsQualityNotice({
        metrics_quality: { status: 'unavailable' },
        database_available: false,
        throughput_available: false,
    });
    assert.match(unavailable, /不显示推算值/);
    assert.match(unavailable, /数据库统计暂不可用/);
    assert.match(unavailable, /实时吞吐暂不可用/);

    assert.match(app, /throughput_available\s*===\s*true\s*\?\s*d\.current_qps\s*:\s*null/);
    assert.match(app, /metrics_quality\?\.status\s*===\s*'valid'\s*\?\s*d\.success_rate_pct\s*:\s*null/);
    assert.match(app, /不代表入库成功率/);
});

test('cluster view renders a stopped zero-throughput state without a fabricated success value', () => {
    let rendered = '';
    const context = vm.createContext({
        esc: value => String(value ?? ''),
        fmt: value => String(Number(value || 0)),
        pageHead: () => '',
        render: value => { rendered = value; },
        bindProxyClusterEvents() {},
    });
    vm.runInContext(sourceSection('function metricValue(', 'async function loadOverview()'), context);
    vm.runInContext(sourceSection('function renderProxyClusterView(', 'function smartParseProxyString('), context);
    context.renderProxyClusterView(
        { mode: 'direct' },
        { running: false, total_qps: 0, buffer_depth: 0, inflight_count: 0, logs: [] },
        null,
    );

    assert.match(rendered, /id="liveQps">0\.0</);
    assert.match(rendered, /id="liveCfRate">—</);
    assert.match(rendered, /指标暂不可用/);
    assert.match(rendered, /集群已停止/);
    assert.doesNotMatch(rendered, /(?:99\.8|99\.79)%/);
});

test('navigation shell exposes the complete top-level page set once', () => {
    const routes = [...html.matchAll(/<button[^>]*class="nav-item"[^>]*data-route="([^"]+)"/g)]
        .map(match => match[1]);
    assert.deepEqual(routes, ['/', '/pipeline', '/proxy', '/persons', '/search', '/charts', '/recent']);
    assert.equal(new Set(routes).size, routes.length);
    assert.match(html, /<nav aria-label="主导航">/);
    assert.match(html, /<main class="main" id="mainContent" tabindex="-1"><\/main>/);
});

function routeHarness(rawPath) {
    const [pathname, query = ''] = rawPath.split('?');
    const calls = [];
    let rendered = '';
    const context = vm.createContext({
        URLSearchParams,
        window: { __TPS_RELEASE_MODE__: 'standard' },
        document: { body: { dataset: {} } },
        dashboardRuntime: { beginPage: () => calls.push('begin') },
        clearTimeout() {},
        searchTimer: null,
        scaleTimer: null,
        closeNav: () => calls.push('close-nav'),
        stopPipelinePoll: () => calls.push('stop-pipeline'),
        stopProxyPoll: () => calls.push('stop-proxy'),
        stopRecentPoll: () => calls.push('stop-recent'),
        parseHash: () => ({ path: pathname, params: new URLSearchParams(query) }),
        setActive: value => calls.push(`active:${value}`),
        location: { hash: `#${rawPath}` },
        lastListHash: '/persons',
        personQuery: '', phoneQuery: '', cityFilter: '', stateFilter: '',
        phoneTypeFilter: 'all', hasWirelessFilter: false, ageMinFilter: '', ageMaxFilter: '',
        sortFilter: 'newest', cursorTrail: [''],
        syncCursorTrail: value => calls.push(`cursor:${value}`),
        loadOverview: () => calls.push('overview'),
        loadPipeline: () => calls.push('pipeline'),
        loadProxyCluster: () => calls.push('proxy'),
        loadPersons: () => calls.push('persons'),
        loadSearchPage: () => calls.push('search'),
        loadCharts: () => calls.push('charts'),
        loadRecent: () => calls.push('recent'),
        loadPersonDetail: () => calls.push('detail'),
        emptyState: title => `empty:${title}`,
        render: value => { rendered = value; },
    });
    vm.runInContext(sourceSection('function route()', "document.querySelectorAll('.nav-item')"), context);
    context.route();
    return { calls, rendered };
}

test('router reaches every page view and rejects malformed detail routes locally', () => {
    const expectations = [
        ['/', 'overview'],
        ['/pipeline', 'pipeline'],
        ['/proxy', 'proxy'],
        ['/persons?page=2', 'persons'],
        ['/search?q=status', 'search'],
        ['/charts', 'charts'],
        ['/recent', 'recent'],
        ['/person/record-1', 'detail'],
        ['/unknown', 'overview'],
    ];
    for (const [route, target] of expectations) {
        const result = routeHarness(route);
        assert.ok(result.calls.includes('begin'), `router must start a page for ${route}`);
        assert.ok(result.calls.includes(`active:${route.split('?')[0]}`), `router must activate ${route}`);
        assert.ok(result.calls.includes(target), `router must dispatch ${route} to ${target}`);
    }

    const malformed = routeHarness('/person/not%20valid');
    assert.equal(malformed.calls.includes('detail'), false);
    assert.equal(malformed.rendered, 'empty:无效档案');
});
