const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const app = fs.readFileSync(path.join(__dirname, '..', 'tools/dashboard-app.js'), 'utf8');

function routeSource() {
    const from = app.indexOf('function route()');
    const to = app.indexOf("document.querySelectorAll('.nav-item')", from);
    assert.ok(from >= 0 && to > from, 'route source must be present');
    return app.slice(from, to);
}

function runRoute(rawPath, releaseMode) {
    const calls = [];
    const location = { hash: `#${rawPath}` };
    const context = vm.createContext({
        URLSearchParams,
        window: { __TPS_RELEASE_MODE__: releaseMode },
        document: { body: { dataset: {} } },
        location,
        history: {
            replaceState(_state, _title, nextHash) {
                calls.push(`replace:${nextHash}`);
                location.hash = nextHash;
            },
        },
        dashboardRuntime: { beginPage: () => calls.push('begin') },
        clearTimeout() {},
        searchTimer: null,
        scaleTimer: null,
        closeNav: () => calls.push('close-nav'),
        stopPipelinePoll: () => calls.push('stop-pipeline'),
        stopProxyPoll: () => calls.push('stop-proxy'),
        stopRecentPoll: () => calls.push('stop-recent'),
        parseHash: () => {
            const [pathname, query = ''] = rawPath.split('?');
            return { path: pathname, params: new URLSearchParams(query) };
        },
        setActive: value => calls.push(`active:${value}`),
        loadOverview: () => calls.push('overview'),
        loadPipeline: () => calls.push('pipeline'),
        loadProxyCluster: () => calls.push('proxy'),
        loadPersons: () => calls.push('persons'),
        loadSearchPage: () => calls.push('search'),
        loadCharts: () => calls.push('charts'),
        loadRecent: () => calls.push('recent'),
        loadPersonDetail: () => calls.push('detail'),
        emptyState: title => `empty:${title}`,
        render: value => calls.push(value),
        lastListHash: '/persons',
        personQuery: '', phoneQuery: '', cityFilter: '', stateFilter: '',
        phoneTypeFilter: 'all', hasWirelessFilter: false, ageMinFilter: '', ageMaxFilter: '',
        sortFilter: 'newest', cursorTrail: [''],
        syncCursorTrail: () => {},
    });
    vm.runInContext(routeSource(), context);
    context.route();
    return { calls, location };
}

test('customer release hashes isolate every control or update route before its loader runs', () => {
    for (const rawPath of [
        '/pipeline', '/pipeline/scale', '/proxy', '/proxy/config', '/cluster', '/control', '/update', '/updates',
    ]) {
        const result = runRoute(rawPath, 'customer');
        assert.equal(result.location.hash, '#/', `${rawPath} must be replaced with the safe overview hash`);
        assert.ok(result.calls.includes('replace:#/'), `${rawPath} must replace browser history`);
        assert.ok(result.calls.includes('active:/'), `${rawPath} must activate the overview`);
        assert.ok(result.calls.includes('overview'), `${rawPath} must load the overview`);
        assert.equal(result.calls.includes('pipeline'), false, `${rawPath} must not load pipeline controls`);
        assert.equal(result.calls.includes('proxy'), false, `${rawPath} must not load proxy controls`);
    }

    const standardPipeline = runRoute('/pipeline', 'standard');
    assert.ok(standardPipeline.calls.includes('pipeline'), 'standard releases keep the pipeline route');
    assert.equal(standardPipeline.calls.includes('overview'), false);
});
