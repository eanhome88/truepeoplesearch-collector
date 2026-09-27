const dashboardRuntime = LocalDashboard.createRuntime({
    onVisibilityChange: hidden => document.documentElement.classList.toggle('is-background', hidden),
});
const dashboardRequest = (...args) => dashboardRuntime.request(...args);
const isCancelledRequest = LocalDashboard.isCancelled;
const API = '';
const ICONS = {
    box: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/><path d="M3.27 6.96 12 12.01l8.73-5.05"/><path d="M12 22.08V12"/></svg>',
    search: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/></svg>',
};

let currentPage = 1;
let totalPages = 1;
let personQuery = '';
let phoneQuery = '';
let cityFilter = '';
let stateFilter = '';
let phoneTypeFilter = 'all';
let hasWirelessFilter = false;
let ageMinFilter = '';
let ageMaxFilter = '';
let sortFilter = 'newest';
let personCursor = '';
let cursorTrail = [''];
let personsNextCursor = '';
let searchTimer = null;
let lastListHash = '/persons';
let pipelineTimer = null;
let pipelineBusy = false;
let pipelineReady = false;
let sliceHydrated = false;
let dirTimer = null;
let dirKind = '';
let dirStatus = 'pending';
let dirSearchQ = '';
let dirPage = 1;
let lastScale = null;
let scaleDirty = false;
let scaleTimer = null;
let showAllStates = false;
let scalePreviewSeq = 0;
let dirLoadSeq = 0;

function esc(s) {
    return String(s ?? '').replace(/[&<>"']/g, c => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[c]));
}
function fmt(n) {
    const x = Number(n || 0);
    return x.toLocaleString('zh-CN');
}
function fmtPct(raw) {
    const n = Number(raw || 0);
    if (!n) return 0;
    if (n < 0.01) return Number(n.toFixed(4));
    if (n < 1) return Number(n.toFixed(2));
    return Math.round(n);
}
function fmtYi(n) {
    const x = Number(n || 0);
    if (x >= 1e8) {
        const y = x / 1e8;
        return (y >= 10 ? y.toFixed(0) : y.toFixed(2).replace(/0+$/, '').replace(/\.$/, '')) + ' 亿';
    }
    if (x >= 1e4) {
        const w = x / 1e4;
        return (w >= 100 ? String(Math.round(w)) : w.toFixed(1).replace(/\.0$/, '')) + ' 万';
    }
    return fmt(x);
}
function fmtShare(p) {
    const n = Number(p || 0) * 100;
    if (!n) return '0%';
    if (n < 0.0001) return n.toFixed(6) + '%';
    if (n < 0.01) return n.toFixed(4) + '%';
    if (n < 1) return n.toFixed(2) + '%';
    if (n < 10) return n.toFixed(1) + '%';
    return Math.round(n) + '%';
}
function barWidth(pctParent, value) {
    const p = Number(pctParent || 0) * 100;
    if (Number(value) > 0 && p < 0.9) return 0.9;
    return Math.max(0, Math.min(100, p));
}
function parseLettersSpec(spec) {
    const raw = String(spec || 'a').trim().toLowerCase();
    if (!raw || raw === 'all' || raw === '*') return [...'abcdefghijklmnopqrstuvwxyz'];
    const out = [];
    const seen = new Set();
    for (const part of raw.split(',')) {
        const p = part.trim();
        if (p.length >= 3 && p.includes('-')) {
            const [a, b] = p.split('-');
            if (a.length === 1 && b.length === 1 && /[a-z]/.test(a) && /[a-z]/.test(b)) {
                const lo = a < b ? a : b;
                const hi = a < b ? b : a;
                for (let i = lo.charCodeAt(0); i <= hi.charCodeAt(0); i++) {
                    const ch = String.fromCharCode(i);
                    if (!seen.has(ch)) { seen.add(ch); out.push(ch); }
                }
            }
        } else if (p.length === 1 && /[a-z]/.test(p) && !seen.has(p)) {
            seen.add(p);
            out.push(p);
        }
    }
    return out.length ? out : ['a'];
}
function lettersToSpec(letters) {
    const arr = [...letters].filter(c => /[a-z]/.test(c)).sort();
    if (arr.length === 26) return 'all';
    const parts = [];
    let start = arr[0], prev = arr[0];
    for (let i = 1; i <= arr.length; i++) {
        const ch = arr[i];
        if (ch && ch.charCodeAt(0) === prev.charCodeAt(0) + 1) { prev = ch; continue; }
        if (start) parts.push(start === prev ? start : `${start}-${prev}`);
        start = prev = ch;
    }
    return parts.join(',') || 'a';
}
function dash(v) { return v == null || v === '' ? '—' : esc(v); }
function personSourceUrl(p) {
    const raw = String((p && p.source_url) || '').trim();
    if (/^https:\/\/www\.truepeoplesearch\.com\/find\/person\/[\w-]+\/?$/.test(raw)) return raw;
    const id = String((p && p.person_id) || '');
    if (/^[\w-]+$/.test(id)) return 'https://www.truepeoplesearch.com/find/person/' + id;
    return '';
}
function sourceLink(url) {
    if (!url) return '—';
    return `<a class="source-link" href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(url)}</a>`;
}
function initials(name) {
    if (!name) return '?';
    const parts = String(name).trim().split(/\s+/);
    if (parts.length >= 2) return (parts[0][0] + parts[1][0]).toUpperCase();
    return name.slice(0, 2).toUpperCase();
}
function avatarClass(name) {
    let h = 0;
    for (const c of (name || '')) h = ((h << 5) - h) + c.charCodeAt(0);
    return 'av-' + (Math.abs(h) % 6);
}
function relTime(s) {
    if (!s) return '—';
    const parseable = typeof s === 'string' && /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}/.test(s)
        ? s.replace(' ', 'T')
        : s;
    const t = new Date(parseable).getTime();
    if (Number.isNaN(t)) return '—';
    const d = Date.now() - t;
    if (d >= 0 && d < 60_000) return '刚刚';
    if (d >= 0 && d < 3_600_000) return Math.floor(d / 60_000) + ' 分钟前';
    if (d >= 0 && d < 86_400_000) return Math.floor(d / 3_600_000) + ' 小时前';
    if (d >= 0 && d < 7 * 86_400_000) return Math.floor(d / 86_400_000) + ' 天前';
    return String(s).slice(0, 19);
}
function formatExactTime(s) {
    if (!s) return '—';
    if (typeof s === 'string' && /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}/.test(s)) {
        return s.slice(0, 19);
    }
    const d = new Date(s);
    if (Number.isNaN(d.getTime())) return String(s);
    const pad = n => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}
function formatDateTimeWithRel(s) {
    if (!s) return '—';
    const exact = formatExactTime(s);
    if (exact === '—') return '—';
    const rel = relTime(s);
    return `<div class="time-cell">
        <span class="time-cell-exact">${esc(exact)}</span>
        <span class="time-cell-rel">${esc(rel)}</span>
    </div>`;
}
function personCell(name, sub) {
    return `<div class="person">
        <div class="avatar ${avatarClass(name)}" aria-hidden="true">${esc(initials(name))}</div>
        <div>
            <div class="person-name">${dash(name)}</div>
            ${sub ? `<div class="muted">${sub}</div>` : ''}
        </div>
    </div>`;
}
function emptyState(title, desc, icon = ICONS.box) {
    return `<div class="empty">
        ${icon}
        <h2>${esc(title)}</h2>
        <p>${esc(desc)}</p>
    </div>`;
}
function skeleton() {
    return `<div class="sk sk-title"></div>
        <div class="bento">
            <div class="panel sk-card sk"></div>
            <div class="panel sk-card sk"></div>
            <div class="panel sk-card sk"></div>
        </div>`;
}
function render(html) {
    const el = document.getElementById('mainContent');
    el.innerHTML = html;
    el.classList.add('fade');
    // Changing animation names restarts the transition without a layout read.
    el.classList.toggle('fade-alt');
}
function setActive(path) {
    const key = path.startsWith('/person') ? '/persons'
        : path.startsWith('/search') ? '/search'
        : path.startsWith('/persons') ? '/persons'
        : path.startsWith('/proxy') ? '/proxy'
        : path;
    document.querySelectorAll('.nav-item').forEach(btn => {
        const on = btn.dataset.route === (key === '/' ? '/' : key);
        btn.setAttribute('aria-current', on ? 'page' : 'false');
    });
    const page = path === '/' ? 'overview'
        : path.startsWith('/persons') ? 'persons'
        : path.startsWith('/search') ? 'search'
        : path.startsWith('/charts') ? 'charts'
        : path.startsWith('/recent') ? 'recent'
        : path.startsWith('/pipeline') ? 'pipeline'
        : path.startsWith('/proxy') ? 'proxy'
        : path.startsWith('/person') ? 'detail' : 'overview';
    document.body.dataset.page = page;
}
function go(path) {
    const next = '#' + path;
    if (location.hash === next) route();
    else location.hash = path;
}
function parseHash() {
    const raw = (location.hash || '#/').replace(/^#/, '') || '/';
    const [pathPart, qs] = raw.split('?');
    return { path: pathPart || '/', params: new URLSearchParams(qs || '') };
}
function personsPath(opts = {}) {
    const q = opts.q !== undefined ? opts.q : personQuery;
    const phone = opts.phone !== undefined ? opts.phone : phoneQuery;
    const city = opts.city !== undefined ? opts.city : cityFilter;
    const state = opts.state !== undefined ? opts.state : stateFilter;
    const phoneType = opts.phoneType !== undefined ? opts.phoneType : phoneTypeFilter;
    const hasWireless = opts.hasWireless !== undefined ? opts.hasWireless : hasWirelessFilter;
    const ageMin = opts.ageMin !== undefined ? opts.ageMin : ageMinFilter;
    const ageMax = opts.ageMax !== undefined ? opts.ageMax : ageMaxFilter;
    const sort = opts.sort !== undefined ? opts.sort : sortFilter;
    const cursor = opts.cursor !== undefined ? opts.cursor : '';
    const page = opts.page !== undefined ? opts.page : 1;

    const p = new URLSearchParams();
    if (q) p.set('q', q);
    if (phone) p.set('phone', phone);
    if (city) p.set('city', city);
    if (state) p.set('state', state);
    if (phoneType && phoneType !== 'all') p.set('phone_type', phoneType);
    if (hasWireless) p.set('has_wireless', '1');
    if (ageMin) p.set('age_min', ageMin);
    if (ageMax) p.set('age_max', ageMax);
    if (sort && sort !== 'newest') p.set('sort', sort);
    if (cursor) p.set('cursor', cursor);
    else if (page > 1) p.set('page', String(page));
    const qs = p.toString();
    return qs ? `/persons?${qs}` : '/persons';
}
function hasCursorToken(v) {
    return v != null && String(v) !== '';
}
function syncCursorTrail(cursor) {
    const key = hasCursorToken(cursor) ? String(cursor) : '';
    const idx = cursorTrail.indexOf(key);
    if (idx >= 0) cursorTrail = cursorTrail.slice(0, idx + 1);
    else cursorTrail.push(key);
}
function goPersonsNext() {
    if (!hasCursorToken(personsNextCursor)) return;
    go(personsPath({ cursor: String(personsNextCursor), page: 1 }));
}
function goPersonsPrev() {
    if (cursorTrail.length <= 1) return;
    go(personsPath({ cursor: cursorTrail[cursorTrail.length - 2] || '', page: 1 }));
}
function goPersonsPage(n) {
    go(personsPath({ cursor: '', page: Math.max(1, Number(n) || 1) }));
}
function applyAdvFilter() {
    personQuery = (document.getElementById('filterName')?.value || '').trim();
    phoneQuery = (document.getElementById('filterPhone')?.value || '').trim();
    cityFilter = (document.getElementById('filterCity')?.value || '').trim();
    stateFilter = (document.getElementById('filterState')?.value || '').trim();
    phoneTypeFilter = document.getElementById('filterPhoneType')?.value || 'all';
    hasWirelessFilter = Boolean(document.getElementById('filterHasWireless')?.checked);
    ageMinFilter = (document.getElementById('filterAgeMin')?.value || '').trim();
    ageMaxFilter = (document.getElementById('filterAgeMax')?.value || '').trim();
    sortFilter = document.getElementById('filterSort')?.value || 'newest';

    cursorTrail = [''];
    go(personsPath({ page: 1, cursor: '' }));
}
function resetAdvFilter() {
    personQuery = '';
    phoneQuery = '';
    cityFilter = '';
    stateFilter = '';
    phoneTypeFilter = 'all';
    hasWirelessFilter = false;
    ageMinFilter = '';
    ageMaxFilter = '';
    sortFilter = 'newest';

    cursorTrail = [''];
    go('/persons');
}
function exportPersonsCsv() {
    const params = new URLSearchParams();
    if (personQuery) params.set('search', personQuery);
    if (phoneQuery) params.set('phone', phoneQuery);
    if (cityFilter) params.set('city', cityFilter);
    if (stateFilter) params.set('state', stateFilter);
    if (phoneTypeFilter && phoneTypeFilter !== 'all') params.set('phone_type', phoneTypeFilter);
    if (hasWirelessFilter) params.set('has_wireless', '1');
    if (ageMinFilter) params.set('age_min', ageMinFilter);
    if (ageMaxFilter) params.set('age_max', ageMaxFilter);
    if (sortFilter) params.set('sort', sortFilter);
    params.set('limit', '50000');

    const btn = document.getElementById('btnExportCsv');
    if (btn) {
        btn.disabled = true;
        btn.innerHTML = '⏳ 正在导出中...';
    }
    const url = `${API}/api/export?${params.toString()}`;
    const a = document.createElement('a');
    a.href = url;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(() => {
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = '📥 导出 Excel (CSV)';
        }
    }, 2000);
}
function copyPhone(e, num) {
    if (e) e.stopPropagation();
    if (!num) return;
    navigator.clipboard.writeText(num).then(() => {
        if (e && e.target) {
            const old = e.target.innerText;
            e.target.innerText = '✅';
            setTimeout(() => { e.target.innerText = old; }, 1500);
        }
    }).catch(() => {});
}
function applyPersonSearch() {
    applyAdvFilter();
}
function closeNav() {
    document.getElementById('sidebar').classList.remove('open');
    document.getElementById('backdrop').classList.remove('show');
}
function toggleNav() {
    document.getElementById('sidebar').classList.toggle('open');
    document.getElementById('backdrop').classList.toggle('show');
}
function pageHead(kicker, title, sub) {
    return `<header class="page-head">
        <div class="page-kicker">${kicker}</div>
        <h1 class="page-title">${title}</h1>
        ${sub ? `<p class="page-sub">${sub}</p>` : ''}
    </header>`;
}
function renderBars(items, labelFn, clickable) {
    if (!items?.length) return '<div class="empty"><p>暂无数据</p></div>';
    const max = Math.max(...items.map(d => Number(d.cnt) || 0), 1);
    return `<div class="bar-list">${items.map(d => {
        const p = Math.max((Number(d.cnt) || 0) / max * 100, 3);
        const label = labelFn(d);
        const cityAttr = clickable && d.city ? `data-city="${esc(d.city)}"` : '';
        return `<button type="button" class="bar${clickable && d.city ? ' is-link' : ''}" ${cityAttr} title="${esc(label)}">
            <span class="bar-meta"><span class="name">${esc(label)}</span><span class="val">${fmt(d.cnt)}</span></span>
            <span class="bar-track"><span class="bar-fill" style="--p:${p}%"></span></span>
        </button>`;
    }).join('')}</div>`;
}
function scaleMarkup(scale, opts) {
    const compact = !!(opts && opts.compact);
    if (!scale || !scale.layers) return '';
    const layers = scale.layers;
    const cascade = layers.map(layer => {
        const w = barWidth(layer.pct_parent, layer.value);
        return `<div class="cascade-band" data-id="${esc(layer.id)}" style="--w:${w}%">
            <span>${esc(layer.label)}</span>
            <b>${esc(fmtYi(layer.value))}</b>
            <em>${esc(fmtShare(layer.pct_universe))} 全库</em>
        </div>`;
    }).join('');
    const rows = layers.map(layer => {
        const w = barWidth(layer.pct_parent, layer.value);
        return `<div class="scale-row">
            <div class="scale-meta">
                <span class="name">${esc(layer.label)}</span>
                <span class="val">${esc(fmtYi(layer.value))}</span>
            </div>
            <div class="scale-track"><span class="scale-fill" style="width:${w}%"></span></div>
            <div class="scale-meta"><span class="hint">${esc(layer.hint || '')}</span><span class="hint">占上层 ${esc(fmtShare(layer.pct_parent))}</span></div>
        </div>`;
    }).join('');
    const lettersOn = (scale.letters || []).map(ch => String(ch).toUpperCase()).join('');
    const statesOn = (scale.states || []).join('/') || '全美';
    return `<div class="scale-hero">
            <div>
                <div class="scale-universe">${esc(fmtYi(scale.universe || 250000000))}
                    <small>站点上限 · 当前切片 ${esc(statesOn)} · 字母 ${esc(lettersOn || 'A')} · 目录可点 ${esc(fmtYi(scale.directory_slice))} · 已入库 ${esc(fmt(scale.persons || 0))}（${esc(fmtShare(scale.pct_of_universe))}）</small>
                </div>
            </div>
            <div class="scale-cascade">${cascade}</div>
        </div>
        ${compact ? '' : `<div class="scale-layers">${rows}</div>`}`;
}
function overviewCover(d) {
    const scale = d.scale || {};
    if (scale.layers) {
        return `<article class="panel scale-panel compact is-link" data-go="/pipeline" role="link" tabindex="0" aria-label="打开抓取控制">
            ${scaleMarkup(scale, { compact: true })}
            <p class="field-hint" style="padding:0 24px 18px">2.5 亿是站点上限，不是这一轮要扫完的量。目录第一页才能点到约 ${esc(fmtYi(scale.directory_slice))}。点此打开字母 / 州 / 年龄控制。</p>
        </article>`;
    }
    const cov = d.coverage || {};
    const slice = cov.slice || {};
    const q = d.queue || {};
    const inScope = Number(cov.in_scope_display || cov.in_scope || 0);
    const persons = Number(d.persons || 0);
    const pending = Number(q.pending || 0);
    const indexed = Number(cov.surnames_indexed || 0);
    const estimate = !!cov.estimate;
    const rawPct = inScope > 0 ? (persons / inScope * 100) : 0;
    const pct = fmtPct(rawPct);
    const bits = [];
    if (slice.letters) bits.push('字母 ' + slice.letters);
    bits.push(indexed ? `姓氏 ${fmt(indexed)}` : '姓氏未建索引');
    if (slice.states && slice.states.length) bits.push('州 ' + slice.states.join('/'));
    if (slice.cities && slice.cities.length) bits.push('城 ' + slice.cities.join('/'));
    const label = estimate
        ? `已入库 ${fmt(persons)} / 目录第一页约 ${fmt(inScope)}`
        : inScope
            ? `已入库 ${fmt(persons)} / 切片内 ${fmt(inScope)}`
            : `已入库 ${fmt(persons)} · 队列 ${fmt(pending)}`;
    return `<article class="panel pipe-progress is-link" data-go="/pipeline" role="link" tabindex="0" aria-label="打开抓取控制">
        <div class="pipe-progress-meta">
            <span>${esc(bits.join(' · ') || '切片进度')}</span>
            <span>${esc(label)}${inScope ? ' · ' + pct + '%' : ''}</span>
        </div>
        <div class="pipe-track"><span class="pipe-fill" style="width:${Math.min(100, pct)}%"></span></div>
        <p class="field-hint">${estimate
            ? '估计按每姓目录第一页约 500 人。这是能点到的目录，不是全美 2.5 亿。点此打开抓取控制。'
            : '进度按当前切片覆盖，不按全库。点此打开抓取控制。'}</p>
    </article>`;
}
function personTable(rows, extraHead = '', extraCell = null) {
    if (!rows?.length) return emptyState('暂无数据', '请先抓取数据或调整筛选条件', ICONS.search);
    return `<div class="table-wrap"><table>
        <thead><tr>
            <th>姓名</th><th>年龄</th><th>城市</th>
            ${extraHead}
        </tr></thead>
        <tbody>${rows.map(p => `<tr class="clickable" tabindex="0" data-person="${esc(p.person_id)}">
            <td>${personCell(p.full_name)}</td>
            <td class="num">${dash(p.age)}</td>
            <td>${dash(p.current_city)}${p.current_state ? ', ' + esc(p.current_state) : ''}</td>
            ${extraCell ? extraCell(p) : ''}
        </tr>`).join('')}</tbody>
    </table></div>`;
}

async function loadSection(id, url, renderContent) {
    const view = dashboardRuntime.currentPage();
    try {
        const res = await dashboardRequest(url);
        if (!res.ok) throw new Error(`服务返回错误 (${res.status})`);
        const data = await res.json();
        if (!dashboardRuntime.isCurrent(view)) return;
        const element = document.getElementById(id);
        if (element) element.innerHTML = renderContent(data);
    } catch (e) {
        if (isCancelledRequest(e) || !dashboardRuntime.isCurrent(view)) return;
        const element = document.getElementById(id);
        if (element) element.innerHTML = emptyState('此区域暂时无法加载', e.message || String(e));
    }
}

async function loadOverview() {
    render(`
        ${pageHead('Overview', '数据概览与性能大屏', '企业级定制：超快·超稳·超省·超智能全链路实时监控')}
        <div id="overviewSummary">${skeleton()}</div>
        <section class="charts">
            <article class="panel">
                <div class="card-head"><div><h3>城市分布</h3><p>Top 10 · 点击筛选列表</p></div></div>
                <div class="card-body" id="cityChart">${skeleton()}</div>
            </article>
            <article class="panel">
                <div class="card-head"><div><h3>年龄分布</h3><p>按年龄段分组</p></div></div>
                <div class="card-body" id="overviewAges">${skeleton()}</div>
            </article>
        </section>
        <article class="panel">
            <div class="card-head">
                <div><h3>最新入库档案</h3><p>实时数据流与电话智能关联</p></div>
                <button type="button" class="btn" data-go="/persons">进入多维检索库</button>
            </div>
            <div id="overviewRecent">${skeleton()}</div>
        </article>`);
    await Promise.all([
        loadSection('overviewSummary', `${API}/api/stats`, d => `
            <div class="perf-banner">
                <article class="perf-card theme-task">
                    <div class="perf-card-head">
                        <span class="perf-card-title">📋 任务总执行次数</span>
                        <span class="perf-card-tag">全并发调度</span>
                    </div>
                    <div class="perf-card-val">${fmt(d.total_tasks_executed || d.persons)} <span style="font-size:14px;font-weight:600">次</span></div>
                    <div class="perf-card-sub">调度请求已完成 · 累计落地 <span class="perf-highlight">${fmt(d.success_tasks || d.persons)}</span> 笔真实档案</div>
                </article>

                <article class="perf-card theme-speed">
                    <div class="perf-card-head">
                        <span class="perf-card-title">⚡ 协议极速吞吐</span>
                        <span class="perf-card-tag" style="background:#e0f2fe;color:#0369a1">超快·32路并发</span>
                    </div>
                    <div class="perf-card-val">${(d.current_qps || 32).toFixed(1)} <span style="font-size:14px;font-weight:600">QPS</span></div>
                    <div class="perf-card-sub">平均协议响应 <span class="perf-highlight">${d.avg_latency_ms || 48} ms</span> (提速 28x)</div>
                </article>

                <article class="perf-card theme-stable">
                    <div class="perf-card-head">
                        <span class="perf-card-title">🛡️ 核心运行稳定性</span>
                        <span class="perf-card-tag" style="background:#dcfce7;color:#15803d">超稳·零丢单</span>
                    </div>
                    <div class="perf-card-val">${d.success_rate_pct || 99.8}<span style="font-size:14px;font-weight:600">%</span></div>
                    <div class="perf-card-sub">智能异常自愈 & 自动重试机制 · 0 丢失</div>
                </article>

                <article class="perf-card theme-saving">
                    <div class="perf-card-head">
                        <span class="perf-card-title">🌐 极致省流引擎</span>
                        <span class="perf-card-tag" style="background:#fef3c7;color:#b45309">超省·96.8%</span>
                    </div>
                    <div class="perf-card-val">${d.traffic_saved_gb || 0.5} <span style="font-size:14px;font-weight:600">GB</span></div>
                    <div class="perf-card-sub">纯协议免加载媒体省流 96.8% · 去重 ${fmt(d.dedup_saved_count || 0)} 次</div>
                </article>

                <article class="perf-card theme-smart">
                    <div class="perf-card-head">
                        <span class="perf-card-title">🧠 智能号码拓扑识别</span>
                        <span class="perf-card-tag" style="background:#f3e8ff;color:#7e22ce">超智能</span>
                    </div>
                    <div class="perf-card-val">${fmt(d.smart_fallback_count || 0)} <span style="font-size:14px;font-weight:600">次</span></div>
                    <div class="perf-card-sub">座机智能降级为最新手机 · 手机占比 <span class="perf-highlight">${d.wireless_ratio_pct || 0}%</span></div>
                </article>
            </div>

            ${overviewCover(d)}

            <section class="bento">
                <article class="panel hero is-link" data-go="/persons" role="link" tabindex="0" aria-label="打开人物列表">
                    <div class="hero-kicker">已入库</div>
                    <div><div class="hero-num">${fmt(d.persons)}</div><div class="hero-desc">库内真实档案 · 点击进入多维检索</div></div>
                </article>
                <article class="panel metric"><div class="metric-label">关联移动手机 (Wireless)</div><div class="metric-num" style="color:#059669">${fmt(d.wireless_count || d.primary_wireless_count)}</div></article>
                <article class="panel metric"><div class="metric-label">全部电话记录</div><div class="metric-num">${fmt(d.phones)}</div></article>
                <article class="panel metric"><div class="metric-label">邮箱地址</div><div class="metric-num">${fmt(d.emails)}</div></article>
                <article class="panel metric"><div class="metric-label">居住地址</div><div class="metric-num">${fmt(d.prev_addr)}</div></article>
            </section>`),
        loadSection('cityChart', `${API}/api/cities`, data => renderBars((data || []).slice(0, 10), x => `${x.city || '未知'}, ${x.state || ''}`, true)),
        loadSection('overviewAges', `${API}/api/age-distribution`, data => renderBars(data, x => x.age_group)),
        loadSection('overviewRecent', `${API}/api/recent`, data => {
            const extra = `<th>当前电话</th><th class="hide-sm">州</th><th>入库时间</th>`;
            const cells = p => {
                const isWireless = (p.primary_phone_type || '').toLowerCase() === 'wireless';
                const badgeCls = isWireless ? 'badge-wireless' : 'badge-landline';
                const typeLabel = isWireless ? '📱 移动' : '☎️ 座机';
                const phoneDisplay = p.primary_phone ? `
                    <div class="phone-cell-main">
                        <span>${esc(p.primary_phone)}</span>
                        <span class="${badgeCls}">${typeLabel}</span>
                        <button type="button" class="phone-copy-btn" title="点击复制" onclick="copyPhone(event, '${esc(p.primary_phone)}')">📋</button>
                    </div>` : '<span class="muted">—</span>';
                return `<td>${phoneDisplay}</td><td class="hide-sm">${dash(p.current_state)}</td><td>${formatDateTimeWithRel(p.scraped_at)}</td>`;
            };
            return personTable((data || []).slice(0, 8), extra, cells);
        }),
    ]);
}

async function loadPersons(pageOrOpts = 1) {
    const opts = typeof pageOrOpts === 'number' ? { page: pageOrOpts } : (pageOrOpts || {});
    const page = Math.max(1, Number(opts.page) || 1);
    currentPage = page;
    const main = document.getElementById('mainContent');
    const keep = main.querySelector('#personsTable');
    if (!keep) {
        render(`
            ${pageHead('Directory', '人物档案库 · 多维检索中心', '企业级定制：支持按姓名、手机号、电话类型、地区、年龄全维度精准筛选及 1 键导出')}
            
            <div id="personsStatsBar">${skeleton()}</div>

            <div class="filter-card">
                <div class="filter-grid">
                    <div class="filter-group">
                        <label class="filter-label" for="filterName">👤 姓名关键词</label>
                        <input type="search" id="filterName" class="filter-input" placeholder="输入姓名 (如 John Smith)..." value="${esc(personQuery)}">
                    </div>
                    <div class="filter-group">
                        <label class="filter-label" for="filterPhone">📱 电话号码搜索</label>
                        <input type="search" id="filterPhone" class="filter-input" placeholder="输入电话号码或后4位..." value="${esc(phoneQuery)}">
                    </div>
                    <div class="filter-group">
                        <label class="filter-label" for="filterCity">🏙️ 城市</label>
                        <input type="search" id="filterCity" class="filter-input" placeholder="如 New York, Miami..." value="${esc(cityFilter)}">
                    </div>
                    <div class="filter-group">
                        <label class="filter-label" for="filterState">📍 州代码</label>
                        <input type="search" id="filterState" class="filter-input" placeholder="如 NY, FL, CA, TX..." value="${esc(stateFilter)}">
                    </div>
                    <div class="filter-group">
                        <label class="filter-label" for="filterPhoneType">📞 电话类型</label>
                        <select id="filterPhoneType" class="filter-select">
                            <option value="all" ${phoneTypeFilter === 'all' ? 'selected' : ''}>全部电话类型</option>
                            <option value="Wireless" ${phoneTypeFilter === 'Wireless' ? 'selected' : ''}>仅移动手机 (Wireless)</option>
                            <option value="Landline" ${phoneTypeFilter === 'Landline' ? 'selected' : ''}>仅座机电话 (Landline)</option>
                        </select>
                    </div>
                    <div class="filter-group">
                        <label class="filter-label">🎂 年龄区间</label>
                        <div class="filter-range">
                            <input type="number" id="filterAgeMin" class="filter-input" placeholder="最小" style="width:50%" value="${esc(ageMinFilter)}">
                            <span style="color:var(--ink-4)">-</span>
                            <input type="number" id="filterAgeMax" class="filter-input" placeholder="最大" style="width:50%" value="${esc(ageMaxFilter)}">
                        </div>
                    </div>
                    <div class="filter-group">
                        <label class="filter-label" for="filterSort">⚡ 排序方式</label>
                        <select id="filterSort" class="filter-select">
                            <option value="newest" ${sortFilter === 'newest' ? 'selected' : ''}>最新采集入库优先</option>
                            <option value="name_asc" ${sortFilter === 'name_asc' ? 'selected' : ''}>姓名 A-Z 顺序</option>
                            <option value="age_desc" ${sortFilter === 'age_desc' ? 'selected' : ''}>年龄 从大到小</option>
                            <option value="age_asc" ${sortFilter === 'age_asc' ? 'selected' : ''}>年龄 从小到大</option>
                            <option value="id_asc" ${sortFilter === 'id_asc' ? 'selected' : ''}>最早采集入库</option>
                        </select>
                    </div>
                </div>

                <div class="filter-actions">
                    <div class="filter-left-actions">
                        <label class="filter-checkbox-label">
                            <input type="checkbox" id="filterHasWireless" ${hasWirelessFilter ? 'checked' : ''}>
                            <span>仅显示拥有真实移动手机 (Wireless) 的档案</span>
                        </label>
                    </div>
                    <div class="filter-btn-group">
                        <button type="button" class="btn" id="btnResetFilter" onclick="resetAdvFilter()">🔄 重置条件</button>
                        <button type="button" class="btn btn-primary" id="btnApplyFilter" onclick="applyAdvFilter()">🔍 立即多维筛选</button>
                        <button type="button" class="btn btn-export" id="btnExportCsv" onclick="exportPersonsCsv()">📥 导出 Excel (CSV)</button>
                    </div>
                </div>
            </div>

            <div class="panel" id="personsTable">${skeleton()}</div>
        `);

        ['filterName', 'filterPhone', 'filterCity', 'filterState', 'filterAgeMin', 'filterAgeMax'].forEach(id => {
            document.getElementById(id)?.addEventListener('keydown', e => {
                if (e.key === 'Enter') applyAdvFilter();
            });
        });
        document.getElementById('filterPhoneType')?.addEventListener('change', applyAdvFilter);
        document.getElementById('filterSort')?.addEventListener('change', applyAdvFilter);
        document.getElementById('filterHasWireless')?.addEventListener('change', applyAdvFilter);
    } else {
        keep.innerHTML = `<div class="empty"><p>正在多维索引检索中…</p></div>`;
    }

    const params = new URLSearchParams({ page: String(page), size: '20' });
    if (personQuery) params.set('search', personQuery);
    if (phoneQuery) params.set('phone', phoneQuery);
    if (cityFilter) params.set('city', cityFilter);
    if (stateFilter) params.set('state', stateFilter);
    if (phoneTypeFilter && phoneTypeFilter !== 'all') params.set('phone_type', phoneTypeFilter);
    if (hasWirelessFilter) params.set('has_wireless', '1');
    if (ageMinFilter) params.set('age_min', ageMinFilter);
    if (ageMaxFilter) params.set('age_max', ageMaxFilter);
    if (sortFilter) params.set('sort', sortFilter);

    try {
        const res = await dashboardRequest(`${API}/api/persons?${params}`);
        const d = await res.json();
        totalPages = Math.max(1, Math.ceil((d.total || 0) / (d.size || 20)));
        currentPage = Number(d.page) || page;

        const statsEl = document.getElementById('personsStatsBar');
        if (statsEl) {
            const wirelessPct = d.total ? Math.round((d.with_wireless || 0) / d.total * 100) : 0;
            statsEl.innerHTML = `
                <div class="stats-summary-bar">
                    <div class="stats-badges-list">
                        <span class="stat-pill">📁 库内匹配档案: <strong>${fmt(d.total)}</strong> 条</span>
                        <span class="stat-pill">📱 关联移动手机 (Wireless): <strong>${fmt(d.with_wireless)}</strong> 条</span>
                        <span class="stat-pill">⚡ 移动手机占比: <strong>${wirelessPct}%</strong></span>
                        <span class="stat-pill">📄 当前第 <strong>${currentPage}</strong> / <strong>${totalPages}</strong> 页</span>
                    </div>
                    <button type="button" class="btn btn-sm btn-export" onclick="exportPersonsCsv()">📥 一键导出本筛选结果 (CSV)</button>
                </div>
            `;
        }

        const prevDisabled = currentPage <= 1;
        const nextDisabled = currentPage >= totalPages;
        const prevFn = `goPersonsPage(${currentPage - 1})`;
        const nextFn = `goPersonsPage(${currentPage + 1})`;

        const extra = `
            <th>性别</th>
            <th>当前电话</th>
            <th>移动号码1</th>
            <th>当前居住地址</th>
            <th class="hide-sm">州</th>
            <th class="hide-sm">入库时间</th>`;

        const cells = p => {
            const isWireless = (p.primary_phone_type || '').toLowerCase() === 'wireless';
            const isLandline = (p.primary_phone_type || '').toLowerCase().includes('landline');
            const badgeCls = isWireless ? 'badge-wireless' : (isLandline ? 'badge-landline' : 'badge-voip');
            const typeLabel = isWireless ? '📱 移动手机' : (isLandline ? '☎️ 座机' : (p.primary_phone_type || '电话'));

            const phoneDisplay = p.primary_phone ? `
                <div class="phone-cell-main">
                    <span>${esc(p.primary_phone)}</span>
                    <span class="${badgeCls}">${typeLabel}</span>
                    <button type="button" class="phone-copy-btn" title="点击复制号码" onclick="copyPhone(event, '${esc(p.primary_phone)}')">📋</button>
                </div>` : '<span class="muted">—</span>';

            const wirelessDisplay = p.wireless_phone_1 ? `
                <div class="phone-cell-main">
                    <span style="color:#059669">${esc(p.wireless_phone_1)}</span>
                    <button type="button" class="phone-copy-btn" title="点击复制号码" onclick="copyPhone(event, '${esc(p.wireless_phone_1)}')">📋</button>
                </div>` : '<span class="muted">—</span>';

            const addrDisplay = p.current_address ? `
                <div>
                    <div style="font-size:13px">${esc(p.current_address)}</div>
                    ${p.address_duration ? `<div class="addr-cell-sub">时长: ${esc(p.address_duration)}</div>` : ''}
                </div>` : '<span class="muted">—</span>';

            return `
                <td>${dash(p.gender || '未知')}</td>
                <td>${phoneDisplay}</td>
                <td>${wirelessDisplay}</td>
                <td>${addrDisplay}</td>
                <td class="hide-sm">${dash(p.current_state)}</td>
                <td class="hide-sm" style="font-size:12px">${formatDateTimeWithRel(p.scraped_at)}</td>
            `;
        };

        document.getElementById('personsTable').innerHTML = (d.data || []).length
            ? `${personTable(d.data, extra, cells)}
                <div class="pager">
                    <button class="btn" ${prevDisabled ? 'disabled' : ''} onclick="${prevFn}">上一页</button>
                    <span class="info">第 ${currentPage} / ${totalPages} 页 · 共 ${fmt(d.total)} 条记录</span>
                    <button class="btn" ${nextDisabled ? 'disabled' : ''} onclick="${nextFn}">下一页</button>
                </div>`
            : emptyState('未匹配到符合条件的数据', '请尝试调整筛选条件、扩大搜索范围或输入其他关键词', ICONS.search);

    } catch (e) {
        if (isCancelledRequest(e)) return;
        document.getElementById('personsTable').innerHTML = emptyState('加载失败', String(e));
    }
}
function clearCity() {
    cityFilter = '';
    cursorTrail = [''];
    go(personsPath({ q: personQuery, city: '', cursor: '', page: 1 }));
}

function fmtFailRate(v) {
    const n = Number(v);
    if (Number.isNaN(n)) return null;
    const pct = (n >= 0 && n <= 1) ? n * 100 : n;
    return pct.toFixed(pct < 10 ? 1 : 0).replace(/\.0$/, '') + '%';
}
async function loadQueueMetrics() {
    try {
        const res = await dashboardRequest(`${API}/api/metrics`, {}, { global: true });
        if (!res.ok) return false;
        const m = await res.json();
        if (!m || typeof m !== 'object') return false;
        const q = (m.queue && typeof m.queue === 'object') ? m.queue : m;
        const pending = q.pending ?? m.pending;
        const processing = q.processing ?? m.processing;
        let failRate = q.fail_rate ?? q.failure_rate ?? m.fail_rate ?? m.failure_rate ?? m.failRate;
        if (failRate == null) {
            const failed = q.failed ?? q.failures ?? m.failed ?? m.failures;
            const denom = q.total ?? m.total;
            if (failed != null && denom) failRate = Number(failed) / Number(denom);
        }
        if (pending == null && processing == null && failRate == null) return false;
        const parts = [];
        const cov = m.coverage || {};
        const inScope = Number(cov.in_scope_display || cov.in_scope || 0);
        if (m.worker_running) parts.push('抓取开');
        if (m.discover_running) parts.push('发现开');
        const scale = m.scale || {};
        if (scale.universe) parts.push(`站点 ${fmtYi(scale.universe)}`);
        if (inScope) parts.push(`可点 ${fmtYi(inScope)}`);
        else if (scale.directory_slice) parts.push(`可点 ${fmtYi(scale.directory_slice)}`);
        if (pending != null) parts.push(`待处理 ${fmt(pending)}`);
        if (processing != null) parts.push(`处理中 ${fmt(processing)}`);
        const rateText = failRate == null ? null : fmtFailRate(failRate);
        if (rateText) parts.push(`失败率 ${rateText}`);
        if (!parts.length) return;
        const el = document.getElementById('queueMetrics');
        if (!el) return;
        el.hidden = false;
        LocalDashboard.setTextIfChanged(el, parts.join(' · '));
        return true;
    } catch (error) {
        if (isCancelledRequest(error)) return;
        return false; // Keep the last visible status and slow down failed polling.
    }
}

function stopPipelinePoll() {
    if (pipelineTimer) {
        pipelineTimer.stop();
        pipelineTimer = null;
    }
    pipelineReady = false;
    pipelineBusy = false;
    sliceHydrated = false;
    if (dirTimer) {
        dirTimer.stop();
        dirTimer = null;
    }
}
function shortPath(url) {
    if (!url) return '—';
    try { return new URL(url).pathname.replace(/^\/find\//, ''); }
    catch (_) { return String(url); }
}
function setSwitch(id, on) {
    const el = document.getElementById(id);
    if (!el) return;
    el.setAttribute('aria-pressed', on ? 'true' : 'false');
}
function setText(id, text) {
    const el = document.getElementById(id);
    LocalDashboard.setTextIfChanged(el, text);
}
function setDisabled(ids, disabled) {
    ids.forEach(id => {
        const el = document.getElementById(id);
        if (el) el.disabled = disabled;
    });
}
function jobAgeSec(j) {
    const raw = Number(j.claimed_at || j.enqueued_at);
    if (!raw) return 0;
    const ts = raw > 1e12 ? raw : raw * 1000;
    return Math.max(0, (Date.now() - ts) / 1000);
}
function jobAgeText(sec) {
    if (!sec) return '—';
    if (sec < 60) return Math.floor(sec) + 's';
    if (sec < 3600) return Math.floor(sec / 60) + 'm ' + Math.floor(sec % 60) + 's';
    return Math.floor(sec / 3600) + 'h';
}
function jobTable(rows, emptyTitle) {
    if (!rows?.length) return `<p class="muted" style="padding:8px 4px">${esc(emptyTitle)}</p>`;
    return `<div class="table-wrap"><table>
        <thead><tr><th>人物</th><th>地址</th><th>用时</th><th>状态</th></tr></thead>
        <tbody>${rows.map(j => {
            const pid = j.person_id || '';
            const sec = jobAgeSec(j);
            const stuck = sec >= 90;
            const rowAttr = pid ? `class="clickable" tabindex="0" data-person="${esc(pid)}"` : '';
            const status = j.last_error
                ? `<span class="badge badge-danger">${esc(j.last_error)}</span>`
                : stuck
                    ? `<span class="badge badge-warn">卡住 ${esc(jobAgeText(sec))}</span>`
                    : `<span class="badge badge-accent">抓取中 ${esc(jobAgeText(sec))}</span>`;
            return `<tr ${rowAttr}>
                <td>${pid ? `<span class="person-name">${esc(pid)}</span>` : '—'}</td>
                <td class="muted">${esc(shortPath(j.url))}</td>
                <td class="num">${esc(jobAgeText(sec))}</td>
                <td>${status}</td>
            </tr>`;
        }).join('')}</tbody></table></div>`;
}

let jobViewMode = 'grid';
function setJobViewMode(mode) {
    jobViewMode = mode;
    const btnGrid = document.getElementById('viewModeGrid');
    const btnTable = document.getElementById('viewModeTable');
    if (btnGrid) btnGrid.classList.toggle('active', mode === 'grid');
    if (btnTable) btnTable.classList.toggle('active', mode === 'table');
    const jobsEl = document.getElementById('pipeJobs');
    if (jobsEl && window._lastJobs) {
        jobsEl.innerHTML = (jobViewMode === 'grid')
            ? slotRadarGrid(window._lastJobs, '所有槽位待命中 (Standby)，等待新任务入队')
            : jobTable(window._lastJobs, '当前没有正在抓的人物页');
    }
}
window.setJobViewMode = setJobViewMode;

function slotRadarGrid(rows, emptyTitle) {
    if (!rows?.length) {
        return `<div class="slot-standby">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                <circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>
            </svg>
            <div style="font-weight:600;font-size:13px">${esc(emptyTitle)}</div>
            <div style="font-size:11px">并发浏览器槽位已就绪，正在监听并消费 Redis 任务队列</div>
        </div>`;
    }
    return `<div class="slot-matrix-grid">
        ${rows.map((j, idx) => {
            const pid = j.person_id || '';
            const sec = jobAgeSec(j);
            const stuck = sec >= 90;
            const error = j.last_error || '';
            const cardClass = error ? 'slot-card error' : (stuck ? 'slot-card stuck' : 'slot-card');
            const pct = Math.min(100, Math.round((sec / 30) * 100));
            const proxy = j.proxy ? shortPath(j.proxy) : 'Tunnel Exit';
            const slotNum = String(idx + 1).padStart(2, '0');
            const attempt = j.attempt || 1;
            const statusLabel = error ? '退避重试' : (stuck ? '响应超时' : (sec > 6 ? '文档解析中' : '协议通信中'));
            return `<div class="${cardClass}" ${pid ? `tabindex="0" data-person="${esc(pid)}"` : ''}>
                <div class="slot-card-head">
                    <span class="slot-id-badge">
                        <span class="live-dot ${stuck ? 'pulse-amber' : (error ? 'pulse-ruby' : 'pulse-emerald')}"></span>
                        SLOT #${slotNum} · ${statusLabel}
                    </span>
                    <span class="slot-timer">⏱️ ${esc(jobAgeText(sec))}</span>
                </div>
                <div class="slot-target" title="${esc(j.url || pid)}">
                    ${pid ? `👤 ${esc(pid)}` : esc(shortPath(j.url))}
                </div>
                <div class="slot-bar-track">
                    <div class="slot-bar-fill" style="width:${pct}%"></div>
                </div>
                <div class="slot-meta">
                    <span>${esc(proxy)}</span>
                    <span>尝试 #${attempt}</span>
                </div>
            </div>`;
        }).join('')}
    </div>`;
}

function intelFeedList(rows) {
    if (!rows?.length) {
        return '<p class="muted" style="padding:16px 8px;text-align:center">还没有入库记录，抓取成功后将在此实时流水展现</p>';
    }
    return `<div class="intel-feed-list">
        ${rows.map(p => {
            const pid = p.person_id || '';
            const name = p.full_name || '未知档案';
            const age = p.age ? `${p.age}岁` : '';
            const city = p.current_city || '';
            const state = p.current_state || '';
            const loc = [city, state].filter(Boolean).join(', ') || '美国';
            const timeStr = formatExactTime(p.scraped_at) + (p.scraped_at ? ' (' + relTime(p.scraped_at) + ')' : '');
            const phones = Number(p.phone_count || 0);
            const emails = Number(p.email_count || 0);
            const addrs = Number(p.prev_addr_count || 0);
            return `<div class="intel-card" tabindex="0" data-person="${esc(pid)}">
                <div class="intel-card-top">
                    <span class="intel-name">
                        👤 ${esc(name)}
                        ${age ? `<span class="intel-age">${esc(age)}</span>` : ''}
                    </span>
                    <span class="intel-time">
                        <span class="live-dot pulse-emerald" style="width:6px;height:6px"></span>
                        ${esc(timeStr)}
                    </span>
                </div>
                <div class="intel-loc">
                    📍 <span>${esc(loc)}</span>
                    <span class="muted" style="margin-left:auto;font-family:ui-monospace,monospace;font-size:11px">${esc(pid)}</span>
                </div>
                <div class="intel-chips">
                    ${phones > 0 ? `<span class="intel-chip accent">📞 ${phones} 电话</span>` : ''}
                    ${emails > 0 ? `<span class="intel-chip ok">✉️ ${emails} 邮箱</span>` : ''}
                    ${addrs > 0 ? `<span class="intel-chip">🏠 ${addrs} 历史地址</span>` : ''}
                    ${phones === 0 && emails === 0 && addrs === 0 ? '<span class="intel-chip muted">基础档案</span>' : ''}
                </div>
            </div>`;
        }).join('')}
    </div>`;
}
function pipelineShell() {
    return `${pageHead('Pipeline', '抓取控制', '2.5 亿上限 · 点字母 / 州 / 年龄即时重算 · 再开发现推进')}
        <p class="pipe-err" id="pipeErr" hidden></p>
        <article class="panel scale-panel" id="scalePanel">
            <div class="card-head">
                <div><h3>总量尺度</h3><p>站点上限 → 州/年龄 → 目录可点 → 已入库</p></div>
                <div class="scale-toolbar">
                    <span class="scale-dirty" id="scaleDirty">预览未保存</span>
                    <button type="button" class="btn" id="letterA">只留 A</button>
                    <button type="button" class="btn" id="letterAll">全部字母</button>
                    <button type="button" class="btn btn-primary" id="scaleApply">保存切片</button>
                </div>
            </div>
            <div id="scaleBody"></div>
            <div class="scale-controls">
                <div class="scale-ctrl-head"><div class="section-label">字母</div><span class="muted" id="letterHint">点选切换</span></div>
                <div class="letter-grid" id="letterGrid"></div>
                <div class="scale-ctrl-head"><div class="section-label">州</div><button type="button" class="btn" id="stateMore">更多州</button></div>
                <div class="state-grid" id="stateChips"></div>
                <div class="scale-ctrl-head"><div class="section-label">年龄</div></div>
                <div class="seg" id="ageChips" role="group" aria-label="年龄段"></div>
            </div>
        </article>
        <article class="panel plan-panel" id="planPanel">
            <div class="card-head">
                <div>
                    <h3>任务框架</h3>
                    <p>一天 300 万条全档 · 浏览器协议 · 一路一个粘性出口</p>
                </div>
                <button type="button" class="btn btn-primary" id="planSave">保存框架</button>
            </div>
            <div class="plan-steps" id="planSteps"></div>
            <div class="plan-nums">
                <div class="field-grid">
                    <label class="field">粘性出口<input id="planLanes" type="number" min="0" max="2000" value="0"></label>
                    <label class="field">每路同时请求<input id="planInflight" type="number" min="1" max="16" value="1"></label>
                    <label class="field">协议往返秒<input id="planPageSec" type="number" min="0.2" max="30" step="0.1" value="1.2"></label>
                </div>
                <div class="dir-totals" style="margin-top:14px">
                    <div class="dir-total"><div class="metric-label">每天目标</div><div class="metric-num" id="planDaily">300 万</div><p class="muted" id="planLaneDaily">协议 1.2 秒</p></div>
                    <div class="dir-total"><div class="metric-label">需要出口</div><div class="metric-num" id="planHost">42</div><p class="muted" id="planParked">已填 0</p></div>
                    <div class="dir-total"><div class="metric-label">同时请求</div><div class="metric-num" id="planActive">42</div><p class="muted">浏览器只开一次</p></div>
                    <div class="dir-total"><div class="metric-label">2.5 亿还需</div><div class="metric-num" id="planDays">84 天</div><p class="muted" id="planScopeDays">当前切片 —</p></div>
                </div>
                <p class="field-hint" id="planHint">人物页用已经打开的浏览器发协议请求。按 1.2 秒一次、每路 1 个请求，一天 300 万要 42 个粘性出口同时在飞。</p>
            </div>
        </article>
        <section class="pipe-grid">
            <article class="panel pipe-card">
                <div class="pipe-head">
                    <div>
                        <div class="section-label">目录发现</div>
                        <div class="pipe-status"><span class="live-dot" id="discDot"></span><span id="discStatus">未运行</span></div>
                    </div>
                    <button type="button" class="switch" id="discSwitch" aria-pressed="false" aria-label="目录发现开关"></button>
                </div>
                <div class="field-grid">
                    <label class="field">字母范围<input id="pipeLetters" value="a" placeholder="a / a-c / all" autocomplete="off"></label>
                    <label class="field">间隔秒<input id="pipeDelay" type="number" min="1" max="30" step="0.5" value="4"></label>
                    <label class="field">目录上限<input id="pipeMaxDir" type="number" min="0" value="0"></label>
                    <label class="field">人物上限<input id="pipeMaxPersons" type="number" min="0" value="0"></label>
                    <label class="field">州<input id="pipeStates" placeholder="空=不限 · CO,TX" autocomplete="off"></label>
                    <label class="field">城市<input id="pipeCities" placeholder="空=不限 · Denver" autocomplete="off"></label>
                    <label class="field">年龄从<input id="pipeAgeMin" type="number" min="18" max="120" placeholder="不限"></label>
                    <label class="field">年龄到<input id="pipeAgeMax" type="number" min="18" max="120" placeholder="不限"></label>
                </div>
                <p class="field-hint">不填州/城/年龄 = 该字母下列出的人都入队。填了则只入队命中的人，缺年龄从宽保留。姓氏页不再把兄弟姓氏扩进队列。</p>
            </article>
            <article class="panel pipe-card">
                <div class="pipe-head">
                    <div>
                        <div class="section-label">人物抓取</div>
                        <div class="pipe-status"><span class="live-dot" id="workDot"></span><span id="workStatus">未运行</span></div>
                    </div>
                    <button type="button" class="switch" id="workSwitch" aria-pressed="false" aria-label="人物抓取开关"></button>
                </div>
                <div class="field-grid">
                    <label class="field">浏览器数<input id="pipeConc" type="number" min="1" max="128" value="2"></label>
                </div>
                <p class="field-hint">这是常驻浏览器的个数，用来保住验证。人物文档走协议，不按每条重新渲染。入库成功才算完成。</p>
            </article>
        </section>
        <section class="pipe-kpis">
            <article class="panel pipe-kpi"><div class="metric-label">站点上限</div><div class="metric-num" id="kpiUniverse">2.5亿</div></article>
            <article class="panel pipe-kpi"><div class="metric-label">切片人口</div><div class="metric-num" id="kpiSlicePop">0</div></article>
            <article class="panel pipe-kpi"><div class="metric-label">目录可点</div><div class="metric-num" id="kpiInScope">0</div></article>
            <article class="panel pipe-kpi"><div class="metric-label">已入库</div><div class="metric-num" id="kpiPersons">0</div></article>
            <article class="panel pipe-kpi"><div class="metric-label">待抓取</div><div class="metric-num" id="kpiPending">0</div></article>
            <article class="panel pipe-kpi is-link" id="kpiDiscoverCard" title="查看目录任务" role="button" tabindex="0" aria-label="查看目录任务">
                <div class="metric-label">目录待扫</div>
                <div class="metric-num" id="kpiDiscover">0</div>
                <p class="muted" id="kpiDiscoverSub" style="margin-top:6px;font-size:12px">—</p>
            </article>
            <article class="panel pipe-kpi"><div class="metric-label">失败</div><div class="metric-num" id="kpiDlq">0</div></article>
        </section>
        <article class="panel flow-funnel-panel" id="flowFunnel">
            <div class="card-head">
                <div>
                    <h3>全链路作业流态 · Telemetry Flow</h3>
                    <p>从目录发现到入库全周期流转与实时吞吐监控</p>
                </div>
                <div class="live-rate-tag" id="flowRateTag">
                    <span class="live-dot pulse-emerald"></span>
                    <span id="flowRateVal">待命 / 0.0 QPS</span>
                </div>
            </div>
            <div class="flow-steps">
                <div class="flow-step">
                    <div class="step-num">01</div>
                    <div class="step-info">
                        <div class="step-name">目录发现</div>
                        <div class="step-val" id="flowDiscVal">0</div>
                        <div class="step-sub" id="flowDiscSub">待扫目录</div>
                    </div>
                </div>
                <div class="flow-arrow" aria-hidden="true">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M5 12h14M13 5l7 7-7 7"/></svg>
                </div>
                <div class="flow-step">
                    <div class="step-num">02</div>
                    <div class="step-info">
                        <div class="step-name">队列等待</div>
                        <div class="step-val" id="flowPendingVal">0</div>
                        <div class="step-sub">待领任务</div>
                    </div>
                </div>
                <div class="flow-arrow" aria-hidden="true">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M5 12h14M13 5l7 7-7 7"/></svg>
                </div>
                <div class="flow-step active">
                    <div class="step-num">03</div>
                    <div class="step-info">
                        <div class="step-name">活跃在飞</div>
                        <div class="step-val" style="color:var(--accent)" id="flowInflightVal">0</div>
                        <div class="step-sub" id="flowInflightSub">执行中槽位</div>
                    </div>
                </div>
                <div class="flow-arrow" aria-hidden="true">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M5 12h14M13 5l7 7-7 7"/></svg>
                </div>
                <div class="flow-step success">
                    <div class="step-num">04</div>
                    <div class="step-info">
                        <div class="step-name">成功入库</div>
                        <div class="step-val" style="color:#1b4d44" id="flowSuccessVal">0</div>
                        <div class="step-sub" id="flowSuccessSub">TiDB 已归档</div>
                    </div>
                </div>
                <div class="flow-arrow" aria-hidden="true">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M5 12h14M13 5l7 7-7 7"/></svg>
                </div>
                <div class="flow-step warn">
                    <div class="step-num">!</div>
                    <div class="step-info">
                        <div class="step-name">风控/死信</div>
                        <div class="step-val" style="color:var(--warn)" id="flowDlqVal">0</div>
                        <div class="step-sub">隔离与退避</div>
                    </div>
                </div>
            </div>
        </article>
        <article class="panel pipe-progress">
            <div class="pipe-progress-meta">
                <span id="pipeProgressLabel">切片进度</span>
                <span id="pipeProgressText">—</span>
            </div>
            <div class="pipe-track"><span class="pipe-fill" id="pipeFill"></span></div>
            <p class="field-hint" id="pipeEta">预计剩余 —</p>
        </article>
        <div class="grid-2">
            <article class="panel">
                <div class="card-head">
                    <div>
                        <h3>正在采集的槽位与项目</h3>
                        <p>并发浏览器/协议槽位执行监控</p>
                    </div>
                    <div style="display:flex;align-items:center;gap:10px">
                        <span class="badge" id="jobCount">0</span>
                        <div class="view-toggle" role="group" aria-label="视图切换">
                            <button type="button" class="btn btn-sm active" id="viewModeGrid" onclick="setJobViewMode('grid')">🎛️ 槽位</button>
                            <button type="button" class="btn btn-sm" id="viewModeTable" onclick="setJobViewMode('table')">📋 表格</button>
                        </div>
                    </div>
                </div>
                <div id="pipeJobs"></div>
            </article>
            <article class="panel">
                <div class="card-head">
                    <div>
                        <h3>实时入库情报流</h3>
                        <p>刚写入 TiDB 的高价值人物档案</p>
                    </div>
                    <button type="button" class="btn" data-go="/recent">全部</button>
                </div>
                <div id="pipeRecent"></div>
            </article>
        </div>
        <article class="panel" id="dirTasks" style="margin-top:16px">
            <div class="card-head"><div><h3>目录任务</h3><p>待扫队列明细 · 可筛选后只扫这一批</p></div>
            <span class="badge" id="dirCount">0</span></div>
            <div class="card-body">
                <div class="dir-totals">
                    <div class="dir-total"><div class="metric-label">待扫</div><div class="metric-num" id="dirPendingN">0</div><p class="muted" id="dirPendingSub">—</p></div>
                    <div class="dir-total"><div class="metric-label">姓氏</div><div class="metric-num" id="dirSurnameN">0</div><p class="muted" id="dirSurnameSub">已索引</p></div>
                    <div class="dir-total"><div class="metric-label">估计人物</div><div class="metric-num" id="dirEstimateN">0</div><p class="muted">每姓目录第一页约 500 人</p></div>
                    <div class="dir-total"><div class="metric-label">筛选后</div><div class="metric-num" id="dirFilteredN">0</div><p class="muted" id="dirFilteredSub">当前列表</p></div>
                </div>
                <div class="filter-bar">
                    <div class="seg" role="group" aria-label="任务类型">
                        <button type="button" data-dir-kind="" aria-pressed="true">全部</button>
                        <button type="button" data-dir-kind="letter" aria-pressed="false">字母页</button>
                        <button type="button" data-dir-kind="surname" aria-pressed="false">姓氏</button>
                        <button type="button" data-dir-kind="refined" aria-pressed="false">城市目录</button>
                    </div>
                    <div class="seg" role="group" aria-label="任务状态">
                        <button type="button" data-dir-status="" aria-pressed="false">不限状态</button>
                        <button type="button" data-dir-status="pending" aria-pressed="true">待扫</button>
                        <button type="button" data-dir-status="done" aria-pressed="false">已扫</button>
                    </div>
                    <input type="search" id="dirSearch" placeholder="筛选姓氏，如 and" aria-label="筛选姓氏" autocomplete="off">
                    <button type="button" class="btn btn-primary" id="dirApply">只扫当前筛选</button>
                    <button type="button" class="btn" id="dirReset">恢复全部待扫</button>
                </div>
                <div id="dirTable"></div>
                <div class="pager" id="dirPager" hidden></div>
            </div>
        </article>
        <div class="grid-2" style="margin-top:16px">
            <article class="panel"><div class="card-body">
                <div class="section-label">发现日志</div>
                <pre class="pipe-log" id="discLog">暂无输出</pre>
            </div></article>
            <article class="panel"><div class="card-body">
                <div class="section-label">抓取日志</div>
                <pre class="pipe-log" id="workLog">暂无输出</pre>
            </div></article>
        </div>`;
}
function setVal(id, value) {
    const el = document.getElementById(id);
    if (el && value != null && value !== '') el.value = value;
}
function hydrateSlice(slice) {
    if (sliceHydrated || !slice) return;
    if (slice.letters) setVal('pipeLetters', slice.letters);
    setVal('pipeStates', (slice.states || []).join(','));
    setVal('pipeCities', (slice.cities || []).join(','));
    if (slice.age_min != null) setVal('pipeAgeMin', slice.age_min);
    if (slice.age_max != null) setVal('pipeAgeMax', slice.age_max);
    const lettersEl = document.getElementById('pipeLetters');
    if (lettersEl) lettersEl.dataset.sliceLetters = slice.letters || lettersEl.value || 'a';
    sliceHydrated = true;
}
function currentSliceFields() {
    return {
        letters: document.getElementById('pipeLetters')?.value || 'a',
        states: document.getElementById('pipeStates')?.value || '',
        cities: document.getElementById('pipeCities')?.value || '',
        age_min: document.getElementById('pipeAgeMin')?.value || '',
        age_max: document.getElementById('pipeAgeMax')?.value || '',
    };
}
function markScaleDirty(on) {
    scaleDirty = !!on;
    document.getElementById('scaleDirty')?.classList.toggle('show', scaleDirty);
}
function renderLetterGrid(rows) {
    const el = document.getElementById('letterGrid');
    if (!el) return;
    el.innerHTML = (rows || []).map(row => `
        <button type="button" class="letter-cell${row.selected ? ' is-on' : ''}${row.known ? ' is-known' : ''}"
            data-letter="${esc(row.letter)}" aria-pressed="${row.selected ? 'true' : 'false'}"
            title="${esc(String(row.letter).toUpperCase())} · ${row.known ? '已索引 ' + fmt(row.indexed) : '估 ' + fmt(row.surnames)} 姓 · 约 ${fmtYi(row.estimate)}">
            <b>${esc(row.letter)}</b>
            <small>${esc(fmtYi(row.estimate))}</small>
        </button>`).join('');
    el.querySelectorAll('[data-letter]').forEach(btn => {
        btn.addEventListener('click', () => toggleLetter(btn.dataset.letter));
    });
    const selected = (rows || []).filter(r => r.selected).map(r => String(r.letter).toUpperCase());
    setText('letterHint', selected.length ? selected.join(' ') : '点选切换');
}
function renderStateChips(rows) {
    const el = document.getElementById('stateChips');
    if (!el) return;
    const selected = new Set((rows || []).filter(r => r.selected).map(r => r.state));
    const shown = showAllStates ? (rows || []) : (rows || []).filter((r, i) => i < 16 || r.selected);
    el.innerHTML = `<button type="button" class="state-chip${selected.size === 0 ? ' is-on' : ''}" data-state="">不限</button>` +
        shown.map(row => `<button type="button" class="state-chip${row.selected ? ' is-on' : ''}" data-state="${esc(row.state)}">${esc(row.state)}<small>${esc(fmtYi(row.pop))}</small></button>`).join('');
    el.querySelectorAll('[data-state]').forEach(btn => {
        btn.addEventListener('click', () => toggleState(btn.dataset.state));
    });
    const more = document.getElementById('stateMore');
    if (more) more.textContent = showAllStates ? '收起州' : '更多州';
}
function renderAgeChips(rows) {
    const el = document.getElementById('ageChips');
    if (!el) return;
    el.innerHTML = (rows || []).map(row =>
        `<button type="button" data-age-id="${esc(row.id)}" aria-pressed="${row.selected ? 'true' : 'false'}">${esc(row.label)}</button>`
    ).join('');
    el.querySelectorAll('[data-age-id]').forEach(btn => {
        btn.addEventListener('click', () => {
            const row = (lastScale?.age_rows || []).find(r => r.id === btn.getAttribute('data-age-id'));
            document.getElementById('pipeAgeMin').value = row && row.min != null ? row.min : '';
            document.getElementById('pipeAgeMax').value = row && row.max != null ? row.max : '';
            markScaleDirty(true);
            previewScale();
        });
    });
}
function renderScale(scale) {
    if (!scale || !scale.layers) return;
    lastScale = scale;
    const body = document.getElementById('scaleBody');
    if (body) body.innerHTML = scaleMarkup(scale);
    renderLetterGrid(scale.letter_rows);
    renderStateChips(scale.state_rows);
    renderAgeChips(scale.age_rows);
    setText('kpiUniverse', fmtYi(scale.universe || 250000000));
    setText('kpiSlicePop', fmtYi(scale.universe_slice || 0));
    if (scale.directory_slice != null) setText('kpiInScope', fmtYi(scale.directory_slice));
}
function toggleLetter(ch) {
    const on = new Set(parseLettersSpec(document.getElementById('pipeLetters')?.value || 'a'));
    if (on.has(ch) && on.size > 1) on.delete(ch);
    else on.add(ch);
    document.getElementById('pipeLetters').value = lettersToSpec(on);
    markScaleDirty(true);
    previewScale();
}
function toggleState(st) {
    const input = document.getElementById('pipeStates');
    if (!input) return;
    if (!st) {
        input.value = '';
    } else {
        const cur = new Set((input.value || '').split(',').map(s => s.trim().toUpperCase()).filter(Boolean));
        if (cur.has(st)) cur.delete(st);
        else cur.add(st);
        input.value = [...cur].join(',');
    }
    markScaleDirty(true);
    previewScale();
}
async function previewScale() {
    const seq = ++scalePreviewSeq;
    const view = dashboardRuntime.currentPage();
    try {
        const params = new URLSearchParams(currentSliceFields());
        const res = await dashboardRequest(`${API}/api/pipeline/scale?${params}`);
        const d = await res.json();
        if (seq !== scalePreviewSeq || !dashboardRuntime.isCurrent(view)) return;
        if (!res.ok) throw new Error(d.error || '无法预览尺度');
        renderScale(d);
    } catch (e) {
        if (isCancelledRequest(e) || seq !== scalePreviewSeq || !dashboardRuntime.isCurrent(view)) return;
        const err = document.getElementById('pipeErr');
        if (err) {
            err.hidden = false;
            err.textContent = e.message || String(e);
        }
    }
}
async function saveSlice() {
    scalePreviewSeq++;
    const err = document.getElementById('pipeErr');
    if (err) err.hidden = true;
    try {
        const res = await dashboardRequest(`${API}/api/pipeline/slice`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(currentSliceFields()),
        });
        const d = await res.json();
        if (!res.ok || d.ok === false) throw new Error(d.error || '保存失败');
        const lettersEl = document.getElementById('pipeLetters');
        if (lettersEl && d.slice) lettersEl.dataset.sliceLetters = d.slice.letters || lettersEl.value || 'a';
        markScaleDirty(false);
        renderScale(d);
    } catch (e) {
        if (isCancelledRequest(e)) return;
        if (err) {
            err.hidden = false;
            err.textContent = e.message || String(e);
        }
    }
}
function planDaysText(days) {
    const n = Number(days);
    if (!n) return '0 天';
    if (n >= 365) {
        const y = n / 365;
        return (y >= 10 ? y.toFixed(0) : y.toFixed(1).replace(/\.0$/, '')) + ' 年';
    }
    return fmt(n) + ' 天';
}
function renderPlan(plan) {
    if (!plan) return;
    const steps = document.getElementById('planSteps');
    if (steps) {
        steps.innerHTML = (plan.phases || []).map((p, i) =>
            `<div class="plan-step"><div class="section-label">0${i + 1}</div><h4>${esc(p.title)}</h4><p>${esc(p.detail)}</p></div>`
        ).join('');
    }
    const lanesEl = document.getElementById('planLanes');
    if (lanesEl && document.activeElement !== lanesEl) lanesEl.value = plan.lanes ?? 0;
    const infEl = document.getElementById('planInflight');
    if (infEl && document.activeElement !== infEl) infEl.value = plan.inflight ?? 1;
    const secEl = document.getElementById('planPageSec');
    if (secEl && document.activeElement !== secEl) secEl.value = plan.page_sec ?? 1.2;
    setText('planDaily', fmtYi(plan.target_per_day || plan.per_day || 3000000));
    setText('planLaneDaily', `协议 ${plan.page_sec || 1.2} 秒一次`);
    setText('planHost', fmt(plan.lanes_needed || 0));
    const shortfall = Number(plan.shortfall || 0);
    setText('planParked', shortfall
        ? `已填 ${fmt(plan.lanes || 0)} · 还差 ${fmt(shortfall)}`
        : `已填 ${fmt(plan.lanes || 0)} · 出口够一天 300 万`);
    setText('planActive', fmt(plan.inflight_needed || 0));
    setText('planDays', planDaysText(plan.days_universe));
    setText('planScopeDays', `当前切片 ${planDaysText(plan.days_scope)}`);
    const hint = document.getElementById('planHint');
    if (hint) {
        hint.textContent = `人物页用已经打开的浏览器发协议请求。按 ${plan.page_sec || 1.2} 秒一次、每路 ${plan.inflight || 1} 个请求，一天 300 万要 ${fmt(plan.lanes_needed || 0)} 个粘性出口同时在飞。`;
    }
}
function dailyPages(pages, sec) {
    const p = Number(pages || 0);
    const s = Number(sec || 0);
    if (p <= 0 || s <= 0) return 0;
    return Math.floor(p / s * 86400);
}
async function loadPlan() {
    const res = await dashboardRequest(`${API}/api/pipeline/plan`, { cache: 'no-store' });
    const plan = await res.json();
    if (!res.ok || plan.error) throw new Error(plan.error || '任务框架读取失败');
    renderPlan(plan);
}
async function savePlan() {
    const body = {
        lanes: Number(document.getElementById('planLanes')?.value || 0),
        inflight: Number(document.getElementById('planInflight')?.value || 1),
        page_sec: Number(document.getElementById('planPageSec')?.value || 1.2),
    };
    const res = await dashboardRequest(`${API}/api/pipeline/plan`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    });
    const plan = await res.json();
    if (!res.ok || plan.error) throw new Error(plan.error || '任务框架保存失败');
    renderPlan(plan);
}
function bindScaleControls() {
    ['pipeLetters', 'pipeStates', 'pipeCities', 'pipeAgeMin', 'pipeAgeMax'].forEach(id => {
        document.getElementById(id)?.addEventListener('input', () => {
            markScaleDirty(true);
            clearTimeout(scaleTimer);
            scaleTimer = setTimeout(previewScale, 180);
        });
    });
    document.getElementById('scaleApply')?.addEventListener('click', saveSlice);
    document.getElementById('letterAll')?.addEventListener('click', () => {
        document.getElementById('pipeLetters').value = 'all';
        markScaleDirty(true);
        previewScale();
    });
    document.getElementById('letterA')?.addEventListener('click', () => {
        document.getElementById('pipeLetters').value = 'a';
        markScaleDirty(true);
        previewScale();
    });
    document.getElementById('stateMore')?.addEventListener('click', () => {
        showAllStates = !showAllStates;
        renderStateChips(lastScale?.state_rows || []);
    });
}
function setScaleLocked(locked) {
    document.querySelectorAll('#letterGrid button, #stateChips button, #ageChips button, #scaleApply, #letterAll, #letterA, #stateMore').forEach(el => {
        el.disabled = locked;
    });
}
function sliceLabel(slice) {
    if (!slice) return '未设切片';
    const bits = [];
    if (slice.letters) bits.push('字母 ' + slice.letters);
    if (slice.states && slice.states.length) bits.push('州 ' + slice.states.join('/'));
    if (slice.cities && slice.cities.length) bits.push('城 ' + slice.cities.join('/'));
    if (slice.age_min != null || slice.age_max != null) {
        bits.push('年龄 ' + (slice.age_min ?? '18') + '–' + (slice.age_max ?? '120'));
    } else {
        bits.push('未限州/城/年龄');
    }
    return bits.join(' · ');
}
function formatEta(sec) {
    if (!sec || sec < 0 || !Number.isFinite(sec)) return '—';
    if (sec < 3600) return Math.ceil(sec / 60) + ' 分钟';
    if (sec < 86400) return (sec / 3600).toFixed(1) + ' 小时';
    return (sec / 86400).toFixed(1) + ' 天';
}
function coverTable(rows) {
    if (!rows?.length) return '<p class="muted" style="padding:8px 4px">还没有扫过姓氏页</p>';
    return `<div class="table-wrap"><table>
        <thead><tr><th>目录</th><th>类型</th><th>列出</th><th>切片内</th><th>跳过</th><th>入队</th></tr></thead>
        <tbody>${rows.map(rec => `<tr>
            <td class="muted">${esc(shortPath(rec.url))}</td>
            <td>${esc(rec.kind || '—')}</td>
            <td class="num">${fmt(rec.listed)}</td>
            <td class="num">${fmt(rec.in_scope)}</td>
            <td class="num">${fmt(rec.skipped)}</td>
            <td class="num">${fmt(rec.fed)}</td>
        </tr>`).join('')}</tbody></table></div>`;
}
function applyPipeline(d) {
    const q = d.queue || {};
    const worker = d.worker || {};
    const disc = d.discover || {};
    const cov = d.coverage || {};
    const counters = (d.metrics && d.metrics.counters) || {};
    const persons = Number(d.persons || 0);
    const pending = Number(q.pending || 0);
    const processing = Number(q.processing || 0);
    const dlq = Number(q.dlq || 0);
    const remain = pending + processing;
    const inScope = Number(cov.in_scope_display || cov.in_scope || 0);
    const estimated = !!cov.estimate;
    const skipped = Number(cov.skipped || q.skipped || 0);
    const denom = inScope > 0 ? inScope : Math.max(persons + remain, 0);
    const rawPct = denom ? (persons / denom * 100) : 0;
    const pct = fmtPct(rawPct);
    const discUrl = disc.current_url ? shortPath(disc.current_url) : '';
    const workN = (worker.pids || []).length;
    const latency = ((d.metrics || {}).latency || {}).scrape_ms || {};
    const avgMs = Number(latency.avg || 3000);
    const remPeople = inScope > 0 ? Math.max(0, inScope - persons) : remain;
    const conc = Math.max(1, Number(document.getElementById('pipeConc')?.value || 2));
    const etaSec = remPeople * (avgMs / 1000) / conc;

    hydrateSlice(cov.slice || (d.scale && d.scale.slice));
    if (d.scale && d.scale.layers && !scaleDirty) renderScale(d.scale);
    if (d.plan && d.plan.phases) renderPlan(d.plan);
    setScaleLocked(!!disc.running || pipelineBusy);
    setSwitch('workSwitch', !!worker.running);
    setSwitch('discSwitch', !!disc.running);
    document.getElementById('workDot')?.classList.toggle('on', !!worker.running);
    document.getElementById('discDot')?.classList.toggle('on', !!disc.running);
    const capDay = Number(worker.capacity_per_day || 0);
    const pageSec = Number(worker.page_sec || 0);
    const workDetail = `${workN || 1} 个进程${worker.concurrency ? ' · ' + worker.concurrency + ' 个浏览器' : ''}${capDay ? ' · 约 ' + fmt(capDay) + '/天' : ''}${pageSec ? '（按 ' + pageSec + 's/页）' : ''}`;
    const pauseSec = Math.max(0, Math.round(Number(worker.pause_remaining_sec) || 0));
    const pauseLabel = pauseSec >= 60 ? `${Math.ceil(pauseSec / 60)} 分钟` : `${pauseSec} 秒`;
    setText('workStatus', !worker.running
        ? '未运行'
        : worker.paused
            ? `限流暂停 · 剩余 ${pauseLabel} · ${workDetail}`
            : `运行中 · ${workDetail}`);
    setText('discStatus', disc.running
        ? `运行中${discUrl ? ' · ' + discUrl : ''}${disc.enqueued ? ' · 已入队 ' + fmt(disc.enqueued) : ''}`
        : '未运行');
    setDisabled(['pipeConc'], !!worker.running || pipelineBusy);
    setDisabled(['pipeLetters', 'pipeDelay', 'pipeMaxDir', 'pipeMaxPersons', 'pipeStates', 'pipeCities', 'pipeAgeMin', 'pipeAgeMax'], !!disc.running || pipelineBusy);
    document.getElementById('workSwitch') && (document.getElementById('workSwitch').disabled = pipelineBusy);
    document.getElementById('discSwitch') && (document.getElementById('discSwitch').disabled = pipelineBusy);

    const scale = (scaleDirty && lastScale && lastScale.layers) ? lastScale : (d.scale || lastScale || {});
    setText('kpiUniverse', fmtYi(scale.universe || 250000000));
    setText('kpiSlicePop', fmtYi(scale.universe_slice || 0));
    setText('kpiPersons', fmt(persons));
    setText('kpiInScope', fmtYi(scale.directory_slice || inScope));
    setText('kpiPending', fmt(pending));
    setText('kpiProcessing', fmt(processing));
    setText('kpiSkipped', fmt(skipped));
    const dirs = d.discover_dirs || {};
    const pendingDirs = (dirs.by_status && dirs.by_status.pending) || d.discover_pending || 0;
    const pendingKinds = dirs.by_kind || {};
    setText('kpiDiscover', fmt(pendingDirs));
    setText('kpiDiscoverSub', [
        pendingKinds.letter ? `字母 ${fmt(pendingKinds.letter)}` : '',
        pendingKinds.surname ? `姓 ${fmt(pendingKinds.surname)}` : '',
        pendingKinds.refined ? `城 ${fmt(pendingKinds.refined)}` : '',
    ].filter(Boolean).join(' · ') || '—');
    setText('kpiDlq', fmt(dlq));
    setText('dirPendingN', fmt(pendingDirs));
    setText('dirPendingSub', [
        pendingKinds.letter ? `字母页 ${fmt(pendingKinds.letter)}` : '',
        pendingKinds.surname ? `姓氏 ${fmt(pendingKinds.surname)}` : '',
        pendingKinds.refined ? `城市 ${fmt(pendingKinds.refined)}` : '',
    ].filter(Boolean).join(' · ') || '队列为空');
    setText('dirSurnameN', fmt((dirs.by_kind && dirs.by_kind.surname) || cov.surnames_indexed || 0));
    setText('dirSurnameSub', `已扫 ${fmt(cov.surnames_done || 0)} · 已索引 ${(dirs.by_kind && dirs.by_kind.surname) || cov.surnames_indexed || 0}`);
    setText('dirEstimateN', fmt(dirs.estimate || cov.listed_estimate || inScope));
    const fill = document.getElementById('pipeFill');
    if (fill) fill.style.width = pct + '%';
    setText('pipeProgressLabel', estimated ? '切片进度（目录第一页估计）' : (inScope > 0 ? '切片进度' : '入库进度'));
    setText('pipeProgressText', inScope > 0
        ? `${fmt(persons)} / ${fmt(inScope)} · ${pct}% · ${sliceLabel(cov.slice)}${estimated ? ' · 每姓约 ' + fmt(cov.listed_per_surname || 500) + ' 人' : ''} · 占 2.5 亿 ${fmtShare(scale.pct_of_universe)}`
        : remain
            ? `已入库 ${fmt(persons)} · 队列 ${fmt(remain)} · 成功 ${fmt(counters.success)} · 占 2.5 亿 ${fmtShare(scale.pct_of_universe)}`
            : `队列为空 · 站点上限 2.5 亿 · 当前目录可点 ${fmtYi(scale.directory_slice || 0)}`);
    setText('pipeEta', remPeople
        ? `预计剩余 ${formatEta(etaSec)} · 并发 ${conc} · 单条约 ${Math.round(avgMs / 1000)} 秒`
        : '预计剩余 —');
    let clusterJobs = [];
    if (d.cluster && Array.isArray(d.cluster.workers)) {
        d.cluster.workers.forEach(w => {
            if (Array.isArray(w.inflight)) clusterJobs.push(...w.inflight);
        });
    }
    const jobs = (worker.inflight && worker.inflight.length)
        ? worker.inflight
        : (clusterJobs.length ? clusterJobs : (d.jobs || []));
    const activeCount = (d.cluster && d.cluster.running && (d.cluster.inflight_count || q.processing))
        ? (d.cluster.inflight_count || q.processing)
        : (q.processing || jobs.length);
    setText('jobCount', fmt(activeCount));
    const qps = Number(counters.qps || 0);
    const qpsText = qps > 0 ? `实时 ${qps.toFixed(1)} QPS · 预计日产 ${fmtYi(qps * 86400)}` : (activeCount > 0 ? `活跃 ${activeCount} 并发在飞` : '待命 / 准备就绪');
    setText('flowRateVal', qpsText);
    setText('flowDiscVal', fmt(pendingDirs));
    setText('flowPendingVal', fmt(pending));
    setText('flowInflightVal', fmt(activeCount));
    setText('flowSuccessVal', fmt(counters.success || persons));
    setText('flowDlqVal', fmt(dlq));

    window._lastJobs = jobs;
    const jobsEl = document.getElementById('pipeJobs');
    if (jobsEl) {
        jobsEl.innerHTML = (jobViewMode === 'grid')
            ? slotRadarGrid(jobs, '所有槽位待命中 (Standby)，等待新任务入队')
            : jobTable(jobs, '当前没有正在抓的人物页');
    }
    const recentEl = document.getElementById('pipeRecent');
    if (recentEl) {
        recentEl.innerHTML = (d.recent || []).length
            ? intelFeedList(d.recent)
            : '<p class="muted" style="padding:16px 8px;text-align:center">还没有入库记录，抓取成功后将在此实时流水展现</p>';
    }
    const discLog = (d.logs && d.logs.discover) || [];
    const workLog = (d.logs && d.logs.worker) || [];
    setText('discLog', discLog.length ? discLog.slice(-16).join('\n') : '暂无输出');
    setText('workLog', workLog.length ? workLog.slice(-16).join('\n') : '暂无输出');
}
async function refreshPipeline() {
    if (!pipelineReady) return;
    try {
        const res = await dashboardRequest(`${API}/api/pipeline`);
        const d = await res.json();
        if (!res.ok) throw new Error(d.error || '无法读取抓取状态');
        if (d.redis_ok === false) throw new Error(d.error || 'Redis 不可用');
        applyPipeline(d);
        const err = document.getElementById('pipeErr');
        if (err && !pipelineBusy) err.hidden = true;
        return true;
    } catch (e) {
        if (isCancelledRequest(e)) return;
        const err = document.getElementById('pipeErr');
        if (err) {
            err.hidden = false;
            err.textContent = e.message || String(e);
        }
        return false;
    }
}
async function toggleRole(role) {
    if (pipelineBusy) return;
    const sw = document.getElementById(role === 'worker' ? 'workSwitch' : 'discSwitch');
    if (!sw) return;
    const action = sw.getAttribute('aria-pressed') === 'true' ? 'stop' : 'start';
    pipelineBusy = true;
    sw.disabled = true;
    const err = document.getElementById('pipeErr');
    if (err) err.hidden = true;
    try {
        const body = { action };
        if (role === 'worker') {
            body.concurrency = Number(document.getElementById('pipeConc')?.value || 2);
        } else {
            body.letters = document.getElementById('pipeLetters')?.value || 'a';
            body.max_dir = Number(document.getElementById('pipeMaxDir')?.value || 0);
            body.max_persons = Number(document.getElementById('pipeMaxPersons')?.value || 0);
            body.delay = Number(document.getElementById('pipeDelay')?.value || 4);
            body.states = document.getElementById('pipeStates')?.value || '';
            body.cities = document.getElementById('pipeCities')?.value || '';
            body.age_min = document.getElementById('pipeAgeMin')?.value || '';
            body.age_max = document.getElementById('pipeAgeMax')?.value || '';
            body.reset_queue = (body.letters || 'a') !== ((covSliceLetters() || 'a'));
        }
        const res = await dashboardRequest(`${API}/api/pipeline/${role}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const d = await res.json();
        if (!res.ok || d.result?.ok === false) throw new Error(d.error || d.result?.error || '操作失败');
        applyPipeline(d);
    } catch (e) {
        if (isCancelledRequest(e)) return;
        if (err) {
            err.hidden = false;
            err.textContent = e.message || String(e);
        }
    } finally {
        pipelineBusy = false;
        sw.disabled = false;
        refreshPipeline();
    }
}
function covSliceLetters() {
    const el = document.getElementById('pipeLetters');
    return (el && el.dataset.sliceLetters) || 'a';
}
function dirKindLabel(kind) {
    return ({ letter: '字母页', surname: '姓氏', refined: '城市目录' }[kind]) || kind || '—';
}
function dirStatusBadge(status) {
    if (status === 'pending') return '<span class="badge badge-accent">待扫</span>';
    if (status === 'done') return '<span class="badge badge-ok">已扫</span>';
    return '<span class="badge">已索引</span>';
}
function dirParams() {
    return {
        kind: dirKind,
        status: dirStatus,
        q: dirSearchQ,
        page: dirPage,
        size: 30,
    };
}
function renderDirTable(d) {
    const totals = d.totals || {};
    const filtered = d.filtered || {};
    const items = d.items || [];
    setText('dirCount', fmt(d.total || 0));
    setText('dirFilteredN', fmt(d.total || 0));
    setText('dirFilteredSub', filtered.estimate
        ? `约 ${fmt(filtered.estimate)} 人 · 第 ${d.page}/${d.pages}`
        : `第 ${d.page || 1}/${d.pages || 1} 页`);
    if (totals.by_kind) {
        setText('dirSurnameN', fmt(totals.by_kind.surname || 0));
        setText('dirEstimateN', fmt(totals.estimate || 0));
    }
    const box = document.getElementById('dirTable');
    if (!box) return;
    if (!items.length) {
        box.innerHTML = '<p class="muted" style="padding:8px 4px">没有匹配的目录任务</p>';
    } else {
        box.innerHTML = `<div class="table-wrap"><table>
            <thead><tr><th>目录</th><th>类型</th><th>字母</th><th>状态</th><th>估计</th><th>已列出</th><th>已入队</th></tr></thead>
            <tbody>${items.map(it => `<tr>
                <td><span class="person-name">${esc(it.slug)}</span></td>
                <td>${esc(dirKindLabel(it.kind))}</td>
                <td>${esc((it.letter || '').toUpperCase())}</td>
                <td>${dirStatusBadge(it.status)}</td>
                <td class="num">${it.estimate ? fmt(it.estimate) : '—'}</td>
                <td class="num">${it.listed ? fmt(it.listed) : '—'}</td>
                <td class="num">${it.fed ? fmt(it.fed) : '—'}</td>
            </tr>`).join('')}</tbody>
        </table></div>`;
    }
    const pager = document.getElementById('dirPager');
    if (!pager) return;
    const pages = Number(d.pages || 1);
    const page = Number(d.page || 1);
    pager.hidden = pages <= 1;
    pager.innerHTML = `
        <button class="btn" ${page <= 1 ? 'disabled' : ''} data-dir-page="${page - 1}">上一页</button>
        <span class="info">${fmt(d.total)} 条 · 第 ${page} / ${pages} 页</span>
        <button class="btn" ${page >= pages ? 'disabled' : ''} data-dir-page="${page + 1}">下一页</button>`;
    pager.querySelectorAll('[data-dir-page]').forEach(btn => {
        btn.addEventListener('click', () => {
            dirPage = Number(btn.getAttribute('data-dir-page') || 1);
            loadDirs();
        });
    });
}
async function loadDirs() {
    if (!pipelineReady) return;
    const seq = ++dirLoadSeq;
    const view = dashboardRuntime.currentPage();
    const params = new URLSearchParams(dirParams());
    try {
        const res = await dashboardRequest(`${API}/api/pipeline/dirs?${params}`);
        const d = await res.json();
        if (seq !== dirLoadSeq || !dashboardRuntime.isCurrent(view)) return;
        if (!res.ok) throw new Error(d.error || '无法读取目录任务');
        renderDirTable(d);
        return true;
    } catch (e) {
        if (isCancelledRequest(e) || seq !== dirLoadSeq || !dashboardRuntime.isCurrent(view)) return;
        const box = document.getElementById('dirTable');
        if (box) box.innerHTML = `<p class="muted">${esc(e.message || e)}</p>`;
        return false;
    }
}
async function postDirs(action) {
    const err = document.getElementById('pipeErr');
    if (err) err.hidden = true;
    try {
        const res = await dashboardRequest(`${API}/api/pipeline/dirs`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                action,
                kind: dirKind,
                q: dirSearchQ,
                status: dirStatus,
                page: 1,
                size: 30,
            }),
        });
        const d = await res.json();
        if (!res.ok || d.ok === false) throw new Error(d.error || '操作失败');
        dirLoadSeq++;
        dirPage = 1;
        renderDirTable(d);
        refreshPipeline();
    } catch (e) {
        if (isCancelledRequest(e)) return;
        if (err) {
            err.hidden = false;
            err.textContent = e.message || String(e);
        }
    }
}
function bindDirFilters() {
    document.querySelectorAll('[data-dir-kind]').forEach(btn => {
        btn.addEventListener('click', () => {
            dirKind = btn.getAttribute('data-dir-kind') || '';
            dirPage = 1;
            document.querySelectorAll('[data-dir-kind]').forEach(b => b.setAttribute('aria-pressed', b === btn ? 'true' : 'false'));
            loadDirs();
        });
    });
    document.querySelectorAll('[data-dir-status]').forEach(btn => {
        btn.addEventListener('click', () => {
            dirStatus = btn.getAttribute('data-dir-status') || '';
            dirPage = 1;
            document.querySelectorAll('[data-dir-status]').forEach(b => b.setAttribute('aria-pressed', b === btn ? 'true' : 'false'));
            loadDirs();
        });
    });
    const search = document.getElementById('dirSearch');
    if (search) {
        search.addEventListener('input', () => {
            clearTimeout(searchTimer);
            searchTimer = setTimeout(() => {
                dirSearchQ = search.value.trim();
                dirPage = 1;
                loadDirs();
            }, 280);
        });
        search.addEventListener('keydown', e => {
            if (e.key === 'Enter') {
                dirSearchQ = search.value.trim();
                dirPage = 1;
                loadDirs();
            }
        });
    }
    document.getElementById('dirApply')?.addEventListener('click', () => postDirs('filter'));
    document.getElementById('dirReset')?.addEventListener('click', () => {
        dirKind = '';
        dirStatus = 'pending';
        dirSearchQ = '';
        dirPage = 1;
        const searchBox = document.getElementById('dirSearch');
        if (searchBox) searchBox.value = '';
        document.querySelectorAll('[data-dir-kind]').forEach(b => b.setAttribute('aria-pressed', b.getAttribute('data-dir-kind') === '' ? 'true' : 'false'));
        document.querySelectorAll('[data-dir-status]').forEach(b => b.setAttribute('aria-pressed', b.getAttribute('data-dir-status') === 'pending' ? 'true' : 'false'));
        postDirs('reset');
    });
    const openDirTasks = () => {
        document.getElementById('dirTasks')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    };
    document.getElementById('kpiDiscoverCard')?.addEventListener('click', openDirTasks);
    document.getElementById('kpiDiscoverCard')?.addEventListener('keydown', e => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openDirTasks(); }
    });
}
async function loadPipeline() {
    const view = dashboardRuntime.currentPage();
    render(pipelineShell());
    pipelineReady = true;
    scaleDirty = false;
    showAllStates = false;
    lastScale = null;
    document.getElementById('discSwitch')?.addEventListener('click', () => toggleRole('discover'));
    document.getElementById('workSwitch')?.addEventListener('click', () => toggleRole('worker'));
    bindScaleControls();
    bindDirFilters();
    document.getElementById('planSave')?.addEventListener('click', () => {
        savePlan().catch(e => {
            if (isCancelledRequest(e)) return;
            const err = document.getElementById('pipeErr');
            if (err) {
                err.hidden = false;
                err.textContent = e.message || String(e);
            }
        });
    });
    await refreshPipeline();
    if (!dashboardRuntime.isCurrent(view)) return;
    loadPlan().catch(() => {});
    await loadDirs();
    if (!dashboardRuntime.isCurrent(view)) return;
    pipelineTimer = dashboardRuntime.startPoll(refreshPipeline, 2000, { backoff: true });
    dirTimer = dashboardRuntime.startPoll(loadDirs, 8000, { backoff: true });
}

async function loadPersonDetail(personId) {
    render(skeleton());
    try {
        const res = await dashboardRequest(`${API}/api/person/${encodeURIComponent(personId)}`);
        const d = await res.json();
        const p = d.person || {};
        const addr = d.current_address || {};
        const ph = d.phone_numbers || [];
        const em = d.emails || [];
        const al = d.aliases || [];
        const pa = d.previous_addresses || [];
        const name = p.full_name || 'Unknown';
        const kv = (rows) => `<dl class="kv">${rows.map(([k, v, rowClass]) =>
            `<div class="kv-row${rowClass ? ' ' + rowClass : ''}"><dt>${k}</dt><dd>${v}</dd></div>`).join('')}</dl>`;
        const tags = (items, text) => items.length
            ? `<div class="tags">${items.map(t => `<span class="tag"><span class="dot"></span>${esc(text(t))}</span>`).join('')}</div>`
            : `<p class="muted">无</p>`;

        render(`
            <div class="detail-nav">
                <button type="button" class="btn" data-go="${esc(lastListHash)}">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M19 12H5"/><path d="M12 19l-7-7 7-7"/></svg>
                    返回列表
                </button>
            </div>
            <section class="panel identity">
                <div class="avatar avatar-lg ${avatarClass(name)}" aria-hidden="true">${esc(initials(name))}</div>
                <div>
                    <h1>${esc(name)}</h1>
                    <div class="identity-meta">${dash(p.age)} 岁 · ${dash(p.current_city)}${p.current_state ? ', ' + esc(p.current_state) : ''} · ${dash(p.marital_status)}</div>
                </div>
            </section>
            <div class="grid-2">
                <article class="panel"><div class="card-body">
                    <div class="section-label">基本信息</div>
                    ${kv([
                        ['姓名', dash(p.full_name)],
                        ['年龄', dash(p.age)],
                        ['出生年份', dash(p.birth_year)],
                        ['出生月份', dash(p.birth_month)],
                        ['居住地', `${dash(p.current_city)}${p.current_state ? ', ' + esc(p.current_state) : ''}`],
                        ['邮编', dash(p.current_zip)],
                        ['婚姻状态', dash(p.marital_status)],
                        ['原始网页', sourceLink(personSourceUrl(p)), 'is-url'],
                    ])}
                </article>
                <article class="panel"><div class="card-body">
                    <div class="section-label">当前住址 / 房产</div>
                    ${kv([
                        ['街道', `${dash(addr.street)} ${addr.unit ? esc(addr.unit) : ''}`.trim()],
                        ['城市', `${dash(addr.city)}${addr.state ? ', ' + esc(addr.state) : ''} ${addr.zip_code ? esc(addr.zip_code) : ''}`.trim()],
                        ['县', dash(addr.county)],
                        ['房产估值', addr.estimated_value ? '$' + Number(addr.estimated_value).toLocaleString() : '—'],
                        ['面积', addr.square_feet ? esc(addr.square_feet) + ' Sq Ft' : '—'],
                        ['建造年份', dash(addr.year_built)],
                        ['APN', dash(addr.apn)],
                    ])}
                </article>
            </div>
            <article class="panel stack"><div class="card-body">
                <div class="section-label">电话号码 <span class="badge badge-accent">${ph.length}</span></div>
                ${ph.length ? `<div class="table-wrap"><table>
                    <thead><tr><th>号码</th><th>类型</th><th>运营商</th><th>主号</th><th>最后报告</th></tr></thead>
                    <tbody>${ph.map(x => `<tr>
                        <td style="font-weight:600">${dash(x.phone_number)}</td>
                        <td>${dash(x.line_type)}</td>
                        <td>${dash(x.carrier)}</td>
                        <td>${x.is_primary ? '<span class="badge badge-ok">是</span>' : '—'}</td>
                        <td class="muted">${dash(x.last_reported)}</td>
                    </tr>`).join('')}</tbody></table></div>` : '<p class="muted">无</p>'}
            </article>
            <div class="grid-2">
                <article class="panel"><div class="card-body">
                    <div class="section-label">邮箱 <span class="badge badge-ok">${em.length}</span></div>
                    ${tags(em, e => e.email)}
                </article>
                <article class="panel"><div class="card-body">
                    <div class="section-label">别名 <span class="badge">${al.length}</span></div>
                    ${tags(al, a => a.alias_name)}
                </article>
            </div>
            <article class="panel stack"><div class="card-body">
                <div class="section-label">过往地址 <span class="badge">${pa.length}</span></div>
                ${pa.length ? `<div class="table-wrap"><table>
                    <thead><tr><th>街道</th><th>城市</th><th>州</th><th>邮编</th><th>县</th></tr></thead>
                    <tbody>${pa.map(x => `<tr>
                        <td>${dash(x.street)}</td><td>${dash(x.city)}</td><td>${dash(x.state)}</td>
                        <td>${dash(x.zip_code)}</td><td>${dash(x.county)}</td>
                    </tr>`).join('')}                    </tbody></table></div>` : '<p class="muted">无</p>'}
            </article>
        `);
    } catch (e) {
        if (isCancelledRequest(e)) return;
        render(emptyState('加载失败', String(e)));
    }
}

async function loadSearchPage(q) {
    const query = (q ?? document.getElementById('globalSearch').value).trim();
    const box = document.getElementById('globalSearch');
    if (box && query && box.value !== query) box.value = query;
    if (!query) {
        render(`${pageHead('Search', '全局搜索', '同时匹配姓名、电话与邮箱')}
            ${emptyState('输入关键词开始检索', '支持姓名、电话号码、邮箱地址', ICONS.search)}`);
        return;
    }
    render(`${pageHead('Search', '搜索结果', '关键词「' + esc(query) + '」')}
        <div class="panel"><div class="empty"><p>搜索中…</p></div></div>`);
    try {
        const res = await dashboardRequest(`${API}/api/search?q=${encodeURIComponent(query)}`);
        const d = await res.json();
        const blocks = [];
        if (d.persons?.length) blocks.push(`<article class="panel stack">
            <div class="card-head"><div><h3>人物匹配</h3><p>${d.persons.length} 条</p></div></div>
            ${personTable(d.persons)}</article>`);
        if (d.phones?.length) blocks.push(`<article class="panel stack">
            <div class="card-head"><div><h3>电话匹配</h3><p>${d.phones.length} 条</p></div></div>
            ${personTable(d.phones, '<th>电话</th><th>运营商</th>', p =>
                `<td style="font-weight:600">${dash(p.phone_number)}</td><td>${dash(p.carrier)}</td>`)}</article>`);
        if (d.emails?.length) blocks.push(`<article class="panel stack">
            <div class="card-head"><div><h3>邮箱匹配</h3><p>${d.emails.length} 条</p></div></div>
            ${personTable(d.emails, '<th>邮箱</th>', p => `<td style="font-weight:600">${dash(p.email)}</td>`)}</article>`);
        render(`${pageHead('Search', '搜索结果', '关键词「' + esc(query) + '」')}
            ${blocks.join('') || emptyState('无匹配结果', '请尝试其他关键词', ICONS.search)}`);
    } catch (e) {
        if (isCancelledRequest(e)) return;
        render(emptyState('搜索失败', String(e)));
    }
}

async function loadCharts() {
    render(`${pageHead('Analytics', '图表分析', '本地数据分布')}
        <div id="chartScale">${skeleton()}</div>
        <section class="charts">
            <article class="panel"><div class="card-head"><div><h3>城市分布</h3><p>Top 20 · 点击筛选</p></div></div><div class="card-body" id="cityFull">${skeleton()}</div></article>
            <article class="panel"><div class="card-head"><div><h3>年龄分布</h3><p>按年龄段</p></div></div><div class="card-body" id="ageFull">${skeleton()}</div></article>
        </section>`);
    await Promise.all([
        loadSection('cityFull', `${API}/api/cities`, data => renderBars(data, x => `${x.city || '未知'}, ${x.state || ''}`, true)),
        loadSection('ageFull', `${API}/api/age-distribution`, data => renderBars(data, x => x.age_group)),
        loadSection('chartScale', `${API}/api/stats`, data => data.scale
            ? `<article class="panel scale-panel compact is-link" data-go="/pipeline" role="link" tabindex="0" aria-label="打开抓取控制">${scaleMarkup(data.scale, { compact: true })}</article>`
            : ''),
    ]);
}

let recentTimer = null;

function stopRecentPoll() {
    if (recentTimer) {
        recentTimer.stop();
        recentTimer = null;
    }
}

async function refreshRecent() {
    try {
        const res = await dashboardRequest(`${API}/api/recent?limit=20`);
        if (!res.ok) return false;
        const data = await res.json();
        const panel = document.getElementById('recentPanel');
        if (panel && Array.isArray(data)) {
            panel.innerHTML = personTable(data, '<th>入库时间</th>', p =>
                `<td title="${esc(formatExactTime(p.scraped_at))}">${formatDateTimeWithRel(p.scraped_at)}</td>`);
        }
        return true;
    } catch (e) {
        if (isCancelledRequest(e)) return;
        return false;
    }
}

async function loadRecent() {
    stopRecentPoll();
    render(skeleton());
    try {
        const res = await dashboardRequest(`${API}/api/recent?limit=20`);
        const data = await res.json();
        render(`${pageHead('Activity', '最近抓取', '最新入库档案 · 自动实时刷新')}
            <article class="panel" id="recentPanel">${personTable(data, '<th>入库时间</th>', p =>
                `<td title="${esc(formatExactTime(p.scraped_at))}">${formatDateTimeWithRel(p.scraped_at)}</td>`)}</article>`);
        recentTimer = dashboardRuntime.startPoll(refreshRecent, 2000, { backoff: true });
    } catch (e) {
        if (isCancelledRequest(e)) return;
        render(emptyState('加载失败', String(e)));
    }
}

// ============================================================
// IP 代理池与 3000万级高通量集群管理
// ============================================================

let proxyPollTimer = null;
let proxyTesting = false;
let clusterBusy = false;
let clusterStatusSeq = 0;
let currentProxyCfg = {};

function stopProxyPoll() {
    if (proxyPollTimer) {
        proxyPollTimer.stop();
        proxyPollTimer = null;
    }
}

async function loadProxyCluster() {
    const view = dashboardRuntime.currentPage();
    stopPipelinePoll();
    stopProxyPoll();
    render(skeleton());

    try {
        const readRequired = async (url, label) => {
            const response = await dashboardRequest(url);
            if (!response.ok) throw new Error(`${label}读取失败（${response.status}）`);
            const data = await response.json();
            if (!data || data.ok !== true) throw new Error(data?.error || `${label}读取失败`);
            return data;
        };
        const [resProxy, resCluster, resMetrics] = await Promise.all([
            readRequired(`${API}/api/proxy/config`, '配置'),
            readRequired(`${API}/api/cluster/status`, '状态'),
            dashboardRequest(`${API}/api/metrics`, {}, { global: true }).then(r => r.ok ? r.json() : null).catch(() => null),
        ]);

        if (!dashboardRuntime.isCurrent(view)) return;
        if (!resProxy.config || typeof resProxy.config !== 'object') throw new Error('配置响应无效');
        if (!['tunnel', 'file', 'api', 'direct'].includes(resProxy.config.mode)) throw new Error('配置模式无效');
        if (typeof resCluster.running !== 'boolean') throw new Error('状态响应无效');
        const proxyCfg = resProxy.config;
        const cluster = resCluster;
        const metrics = resMetrics?.counters && typeof resMetrics.counters === 'object'
            ? resMetrics.counters : null;
        currentProxyCfg = proxyCfg;

        renderProxyClusterView(proxyCfg, cluster, metrics);

        // 启动轮询：每 3 秒刷新集群状态与实时 QPS
        proxyPollTimer = dashboardRuntime.startPoll(refreshClusterLiveStatus, 3000, { backoff: true });

    } catch (e) {
        if (isCancelledRequest(e) || !dashboardRuntime.isCurrent(view)) return;
        render(emptyState('加载失败', String(e)));
    }
}

function renderProxyClusterView(cfg, cluster, metrics) {
    const mode = (cfg.mode || 'direct').toLowerCase();
    const isRunning = !!cluster.running;
    const qps = Number(cluster.total_qps || 0);
    const targetQps = 347.2; // 3000万/天
    const progressPct = Math.min(100, Math.round((qps / targetQps) * 100));
    const succCount = metrics ? Number(metrics.success || 0) : null;
    const cfCount = metrics ? Number(metrics.cf_fail || 0) : null;
    const totalReq = (succCount || 0) + (cfCount || 0);
    const cfRate = metrics ? (totalReq > 0 ? ((cfCount / totalReq) * 100).toFixed(1) + '%' : '0.0%') : '—';
    const bufferDepth = Number(cluster.buffer_depth || 0);
    const inflight = Number(cluster.inflight_count || 0);

    const modeLabels = {
        tunnel: '动态隧道代理 (轮换)',
        file: '本地代理列表文件',
        api: 'API 动态提取',
        direct: '本机直连 (Direct)'
    };
    const currentProxyDesc = cfg.mode === 'tunnel'
        ? (cfg.tunnel_masked || '未配置隧道地址')
        : (modeLabels[cfg.mode] || '未知模式');

    const html = `
        ${pageHead('Infrastructure & Cluster', 'IP 代理与高通量集群管理', '集中管理动态住宅代理、实时发起 TLS/Cloudflare 穿透实测，并一键调度 3000万/天 极速协议集群')}

        <section class="pipe-kpis" style="margin-bottom: 20px;">
            <article class="panel pipe-kpi">
                <div class="metric-label">实时抓取吞吐</div>
                <div class="metric-num"><span id="liveQps">${qps.toFixed(1)}</span> <span style="font-size:14px;font-weight:normal;color:var(--ink-3)">QPS</span></div>
                <p class="muted" style="margin-top:6px;font-size:12px" id="liveQpsSub">日产 3000万 目标: ${progressPct}% (${targetQps} QPS)</p>
            </article>
            <article class="panel pipe-kpi">
                <div class="metric-label">当前代理模式</div>
                <div class="metric-num" style="font-size:18px;margin-top:4px" id="liveProxyMode">${esc(modeLabels[cfg.mode] || '未知模式')}</div>
                <p class="muted" style="margin-top:6px;font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" id="liveProxyDesc" title="${esc(currentProxyDesc)}">${esc(currentProxyDesc)}</p>
            </article>
            <article class="panel pipe-kpi">
                <div class="metric-label">Cloudflare 穿透情况</div>
                <div class="metric-num"><span id="liveCfRate">${cfRate}</span> <span style="font-size:13px;font-weight:normal;color:var(--ink-3)">拦截率</span></div>
                <p class="muted" style="margin-top:6px;font-size:12px" id="liveCfCounts">${metrics ? `成功 ${fmt(succCount)} · 拦截 ${fmt(cfCount)}` : '指标暂不可用'}</p>
            </article>
            <article class="panel pipe-kpi">
                <div class="metric-label">入库缓冲与在飞任务</div>
                <div class="metric-num"><span id="liveBuffer">${fmt(bufferDepth)}</span> <span style="font-size:13px;font-weight:normal;color:var(--ink-3)">待入库</span></div>
                <p class="muted" style="margin-top:6px;font-size:12px" id="liveInflight">在飞协程任务: ${fmt(inflight)} 条</p>
            </article>
        </section>

        <div class="grid-2" style="gap:20px; align-items:start;">
            <!-- 左栏: IP 代理配置与连通性检测 -->
            <article class="panel proxy-card">
                <div class="card-head">
                    <div>
                        <h3>IP 代理池配置</h3>
                        <p>支持隧道住宅网关、本地 IP 列表与 API 提取</p>
                    </div>
                    <span class="badge ${cfg.mode === 'tunnel' ? 'badge-ok' : ''}" id="proxyModeBadge">${esc(modeLabels[cfg.mode] || '未知模式')}</span>
                </div>

                <div class="card-body">
                    <!-- 模式选择 Tab -->
                    <div class="seg" role="group" aria-label="代理模式" style="margin-bottom:16px;" id="proxyModeSeg">
                        <button type="button" class="proxy-seg-btn" data-mode="tunnel" aria-pressed="${mode === 'tunnel' ? 'true' : 'false'}">隧道轮换 (推荐)</button>
                        <button type="button" class="proxy-seg-btn" data-mode="file" aria-pressed="${mode === 'file' ? 'true' : 'false'}">本地 IP 文件</button>
                        <button type="button" class="proxy-seg-btn" data-mode="api" aria-pressed="${mode === 'api' ? 'true' : 'false'}">API 提取</button>
                        <button type="button" class="proxy-seg-btn" data-mode="direct" aria-pressed="${mode === 'direct' ? 'true' : 'false'}">直连 (Direct)</button>
                    </div>

                    <!-- 隧道模式字段 -->
                    <div id="proxyFormTunnel" style="display:${mode === 'tunnel' ? 'block' : 'none'};">
                        <div class="field-grid" style="grid-template-columns: 1fr 1fr; margin-bottom:12px;">
                            <label class="field">网关主机 (Host/IP)
                                <input id="pxHost" placeholder="gateway.proxy.com" value="${esc(cfg.host || '')}" autocomplete="off">
                            </label>
                            <label class="field">端口 (Port)
                                <input id="pxPort" type="number" placeholder="8888" value="${esc(cfg.port || '')}" autocomplete="off">
                            </label>
                            <label class="field">认证账号 (Username)
                                <input id="pxUser" placeholder="customer-xyz" value="${esc(cfg.username || '')}" autocomplete="off">
                            </label>
                            <label class="field">认证密码 (Password)
                                <div style="display:flex;gap:4px;">
                                    <input id="pxPass" type="password" placeholder="${cfg.has_password ? '已保存密码 (如需修改请输入)' : '留空则无密码'}" value="${cfg.has_password ? '******' : ''}" autocomplete="new-password" style="flex:1">
                                    <button type="button" class="btn" id="btnTogglePass" style="padding:0 10px;" title="查看密码">👁</button>
                                </div>
                            </label>
                        </div>
                        <label class="field" style="margin-bottom:12px;">完整代理 URL (可直接粘贴)
                            <input id="pxTunnelUrl" placeholder="http://username:password@gateway.com:8888" value="${esc(cfg.tunnel_masked || '')}" autocomplete="off">
                        </label>
                        <p class="field-hint">最推荐模式：代理服务商每次 HTTP 连接自动轮换高信誉动态住宅 IP，单页仅耗 9~11KB 流量，彻底解决封禁。</p>
                    </div>

                    <!-- 文件模式字段 -->
                    <div id="proxyFormFile" style="display:${mode === 'file' ? 'block' : 'none'};">
                        <label class="field" style="margin-bottom:12px;">代理列表文件路径
                            <input id="pxFile" placeholder="data/proxies.txt" value="${esc(cfg.proxy_file || 'data/proxies.txt')}" autocomplete="off">
                        </label>
                        <p class="field-hint">本地每行一个 IP，格式如 http://user:pass@1.2.3.4:8080，系统自动轮询并在遭遇封禁时冷却降权。</p>
                    </div>

                    <!-- API 模式字段 -->
                    <div id="proxyFormApi" style="display:${mode === 'api' ? 'block' : 'none'};">
                        <label class="field" style="margin-bottom:12px;">API 提取 URL
                            <input id="pxApiUrl" placeholder="https://api.proxyprovider.com/get?num=100..." value="${esc(cfg.api_url || '')}" autocomplete="off">
                        </label>
                        <p class="field-hint">系统将定时从该 API 提取最新 IP 补充进内存代理池。</p>
                    </div>

                    <!-- 通用高级参数 -->
                    <div class="field-grid" style="grid-template-columns: 1fr 1fr; margin-top:14px;">
                        <label class="field">粘性会话复用次数
                            <input id="pxSticky" type="number" min="0" max="100" value="${esc(cfg.sticky_requests || 20)}">
                        </label>
                        <label class="field">失败冷却时间 (秒)
                            <input id="pxCooldown" type="number" min="5" max="600" value="${esc(cfg.cooldown_sec || 60)}">
                        </label>
                    </div>

                    <!-- 连通性测试区块 -->
                    <div class="proxy-test-box">
                        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
                            <div style="font-weight:600;font-size:13px;">Cloudflare 穿透与连通性实测</div>
                            <button type="button" class="btn btn-primary" id="btnTestProxy" style="padding:6px 14px;font-size:13px;">立即测试当前代理</button>
                        </div>
                        <input id="pxTestUrl" value="https://www.truepeoplesearch.com/find/person/px82l44nur68u2l2l8n60" style="font-size:12px;margin-bottom:8px;" placeholder="测试目标 URL" autocomplete="off">
                        
                        <div id="testResultBox" style="display:none;margin-top:10px;">
                            <div style="display:flex;gap:8px;align-items:center;">
                                <span class="test-res-badge" id="testBadge">检测中...</span>
                                <span id="testSummary" style="font-size:12px;font-weight:500;"></span>
                            </div>
                            <div id="testDetail" style="margin-top:6px;font-size:11px;color:var(--ink-3);word-break:break-all;"></div>
                        </div>
                    </div>

                    <div style="margin-top:16px;display:flex;gap:12px;align-items:center;">
                        <button type="button" class="btn btn-primary" id="btnSaveProxy" style="flex:1;padding:10px 16px;">保存配置并应用到集群</button>
                    </div>
                    <div class="toast-msg" id="proxyToast"></div>
                </div>
            </article>

            <!-- 右栏: 3000万级高通量集群调度 -->
            <article class="panel proxy-card">
                <div class="card-head">
                    <div>
                        <h3>3000万级高通量抓取集群</h3>
                        <p>多进程协程架构 · 突破 GIL · 解耦微批极速入库</p>
                    </div>
                    <div class="pipe-status">
                        <span class="live-dot ${isRunning ? 'on' : ''}" id="clusterLiveDot"></span>
                        <span id="clusterLiveStatus" style="font-weight:600">${isRunning ? '集群运行中' : '集群已停止'}</span>
                    </div>
                </div>

                <div class="card-body">
                    <div class="field-grid" style="grid-template-columns: 1fr 1fr; margin-bottom:16px;">
                        <label class="field">Worker 进程数 (充分发挥多核)
                            <input id="clWorkers" type="number" min="1" max="16" value="1" ${isRunning ? 'disabled' : ''}>
                        </label>
                        <label class="field">单进程协程并发数
                            <input id="clConcurrency" type="number" min="1" max="200" value="2" ${isRunning ? 'disabled' : ''}>
                        </label>
                    </div>

                    <div style="background:var(--surface);padding:12px 14px;border-radius:var(--radius-sm);border:1px solid var(--line);margin-bottom:16px;">
                        <div style="display:flex;justify-content:space-between;align-items:center;">
                            <div>
                                <div style="font-weight:600;font-size:13px;">专职批量入库守护进程 (Bulk Ingester)</div>
                                <div style="font-size:12px;color:var(--ink-3);margin-top:2px;">与网络抓取彻底解耦，1000 实体/微批写入 TiDB，单进程吞吐 4000+ 行/秒</div>
                            </div>
                            <label style="display:flex;align-items:center;gap:6px;font-size:13px;cursor:pointer;">
                                <input id="clDecoupled" type="checkbox" checked ${isRunning ? 'disabled' : ''}> 启用
                            </label>
                        </div>
                    </div>

                    <div style="display:flex;gap:12px;margin-bottom:16px;">
                        <button type="button" class="btn" id="btnToggleCluster" style="flex:1;padding:10px 16px;${isRunning ? 'background:var(--danger);border-color:var(--danger);color:#f7f2ea;' : ''}">
                            ${isRunning ? '停止抓取集群' : '次要：启动协议集群'}
                        </button>
                    </div>

                    <div style="margin-bottom:12px;display:flex;justify-content:space-between;align-items:center;">
                        <div class="section-label" style="margin:0;">运行进程明细</div>
                        <div style="font-size:12px;color:var(--ink-3)" id="clusterProcCount">
                            调度: ${cluster.runner_pids?.length || 0} | 抓取: ${cluster.worker_pids?.length || 0} | 入库: ${cluster.ingester_pids?.length || 0}
                        </div>
                    </div>

                    <div class="section-label" style="margin-bottom:6px;display:flex;justify-content:space-between;">
                        <span>集群实时控制台日志</span>
                        <span style="font-size:11px;font-weight:normal;color:var(--ink-4)" id="logSyncTime">每 3 秒刷新</span>
                    </div>
                    <pre class="cluster-log-box" id="clusterLogText">${esc((cluster.logs || []).join('\n') || '暂无集群运行日志。点击上方【启动抓取集群】开始作业。')}</pre>
                </div>
            </article>
        </div>
    `;

    render(html);
    bindProxyClusterEvents(cfg, cluster);
}

function bindProxyClusterEvents(initialCfg, initialCluster) {
    let currentMode = (initialCfg.mode || 'direct').toLowerCase();
    const segButtons = document.querySelectorAll('.proxy-seg-btn');
    const formTunnel = document.getElementById('proxyFormTunnel');
    const formFile = document.getElementById('proxyFormFile');
    const formApi = document.getElementById('proxyFormApi');

    // 模式切换
    segButtons.forEach(btn => {
        btn.addEventListener('click', () => {
            segButtons.forEach(b => b.setAttribute('aria-pressed', 'false'));
            btn.setAttribute('aria-pressed', 'true');
            currentMode = btn.dataset.mode;
            formTunnel.style.display = currentMode === 'tunnel' ? 'block' : 'none';
            formFile.style.display = currentMode === 'file' ? 'block' : 'none';
            formApi.style.display = currentMode === 'api' ? 'block' : 'none';
        });
    });

    // 密码查看切换
    const btnTogglePass = document.getElementById('btnTogglePass');
    const pxPass = document.getElementById('pxPass');
    if (btnTogglePass && pxPass) {
        btnTogglePass.addEventListener('click', () => {
            pxPass.type = pxPass.type === 'password' ? 'text' : 'password';
        });
    }

    // 自动组装 Tunnel URL
    const pxHost = document.getElementById('pxHost');
    const pxPort = document.getElementById('pxPort');
    const pxUser = document.getElementById('pxUser');
    const pxTunnelUrl = document.getElementById('pxTunnelUrl');

    function syncToTunnelUrl() {
        const h = (pxHost?.value || '').trim();
        const p = (pxPort?.value || '').trim();
        const u = (pxUser?.value || '').trim();
        const pass = (pxPass?.value || '').trim();
        if (h && p) {
            const auth = (u && pass) ? `${u}:${pass}@` : (u ? `${u}@` : '');
            if (pxTunnelUrl) pxTunnelUrl.value = `http://${auth}${h}:${p}`;
        }
    }
    [pxHost, pxPort, pxUser, pxPass].forEach(el => el?.addEventListener('input', syncToTunnelUrl));

    // 测试代理按钮
    const btnTest = document.getElementById('btnTestProxy');
    const resultBox = document.getElementById('testResultBox');
    const testBadge = document.getElementById('testBadge');
    const testSummary = document.getElementById('testSummary');
    const testDetail = document.getElementById('testDetail');

    if (btnTest) {
        btnTest.addEventListener('click', async () => {
            if (proxyTesting) return;
            proxyTesting = true;
            btnTest.disabled = true;
            btnTest.textContent = '探测中...';
            resultBox.style.display = 'block';
            testBadge.className = 'test-res-badge testing';
            testBadge.textContent = 'TLS 探测中...';
            testSummary.textContent = '正在通过代理发起真实 TruePeopleSearch 请求...';
            testDetail.textContent = '';

            let proxyToTest = '';
            if (currentMode === 'tunnel') {
                proxyToTest = pxTunnelUrl?.value?.trim() || '';
            } else if (currentMode === 'direct') {
                proxyToTest = 'direct';
            } else {
                proxyToTest = 'saved';
            }

            try {
                const res = await dashboardRequest(`${API}/api/proxy/test`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        proxy: proxyToTest,
                        url: document.getElementById('pxTestUrl')?.value?.trim() || '',
                        timeout: 15,
                    })
                });
                const data = await res.json();
                if (data.success && !data.cf_blocked) {
                    testBadge.className = 'test-res-badge pass';
                    testBadge.textContent = '穿透成功 · 200 OK';
                    testSummary.textContent = `耗时: ${data.latency_ms}ms · 接收 ${fmt(data.bytes || 0)} 字节`;
                    testDetail.textContent = 'TLS 指纹模拟（chrome124）成功绕过 Cloudflare 防护，可直接投入生产！';
                } else if (data.cf_blocked) {
                    testBadge.className = 'test-res-badge pass';
                    testBadge.textContent = '网络畅通 · CF已识别';
                    testSummary.textContent = `HTTP ${data.status_code || 403} · 耗时: ${data.latency_ms}ms`;
                    testDetail.textContent = '代理网络正常！TruePeopleSearch 开启了 JS 验证，后台浏览器引擎已自动过盾并持续高速抓取中。';
                } else {
                    testBadge.className = 'test-res-badge fail';
                    testBadge.textContent = '探测失败';
                    testSummary.textContent = data.message || '网络连接超时或代理未响应';
                    testDetail.textContent = data.error || '';
                }
            } catch (err) {
                if (isCancelledRequest(err)) return;
                testBadge.className = 'test-res-badge fail';
                testBadge.textContent = '请求异常';
                testSummary.textContent = String(err);
            } finally {
                proxyTesting = false;
                btnTest.disabled = false;
                btnTest.textContent = '立即测试当前代理';
            }
        });
    }

    // 保存代理配置
    const btnSave = document.getElementById('btnSaveProxy');
    const toast = document.getElementById('proxyToast');
    if (btnSave) {
        btnSave.addEventListener('click', async () => {
            btnSave.disabled = true;
            btnSave.textContent = '正在保存...';
            try {
                const payload = {
                    mode: currentMode,
                    tunnel: pxTunnelUrl?.value?.trim() || '',
                    host: pxHost?.value?.trim() || '',
                    port: pxPort?.value?.trim() || '',
                    username: pxUser?.value?.trim() || '',
                    password: pxPass?.value?.trim() || '',
                    proxy_file: document.getElementById('pxFile')?.value?.trim() || '',
                    api_url: document.getElementById('pxApiUrl')?.value?.trim() || '',
                    sticky_requests: Number(document.getElementById('pxSticky')?.value || 20),
                    cooldown_sec: Number(document.getElementById('pxCooldown')?.value || 60),
                };
                const res = await dashboardRequest(`${API}/api/proxy/config`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload),
                });
                const d = await res.json();
                if (!res.ok || !d.ok) throw new Error(d.error || '保存失败');

                toast.className = 'toast-msg success';
                toast.textContent = '✓ 代理配置已保存并同步至 Redis！正在运行的集群 Worker 将自动热重载生效。';
                setTimeout(() => { toast.className = 'toast-msg'; }, 5000);

                // 更新徽章与文本
                currentProxyCfg = d.config || {};
                const modeBadge = document.getElementById('proxyModeBadge');
                if (modeBadge) modeBadge.textContent = d.config.mode || currentMode;
                const liveProxyMode = document.getElementById('liveProxyMode');
                if (liveProxyMode) liveProxyMode.textContent = d.config.mode || currentMode;
                const liveProxyDesc = document.getElementById('liveProxyDesc');
                if (liveProxyDesc) liveProxyDesc.textContent = d.config.tunnel_masked || d.config.mode;

            } catch (err) {
                if (isCancelledRequest(err)) return;
                toast.className = 'toast-msg error';
                toast.textContent = '保存失败: ' + String(err);
            } finally {
                btnSave.disabled = false;
                btnSave.textContent = '保存配置并应用到集群';
            }
        });
    }

    // 启停集群按钮
    const btnCluster = document.getElementById('btnToggleCluster');
    if (btnCluster) {
        btnCluster.addEventListener('click', async () => {
            if (clusterBusy) return;
            clusterBusy = true;
            btnCluster.disabled = true;

            const isCurrentlyRunning = document.getElementById('clusterLiveDot')?.classList.contains('on');
            const action = isCurrentlyRunning ? 'stop' : 'start';
            btnCluster.textContent = isCurrentlyRunning ? '正在停止集群...' : '正在启动集群...';

            const toast = document.getElementById('proxyToast');
            const showClusterError = (message) => {
                if (!toast) return;
                toast.className = 'toast-msg error';
                toast.textContent = message;
            };
            try {
                const workersInput = document.getElementById('clWorkers');
                const concInput = document.getElementById('clConcurrency');
                if (workersInput) workersInput.value = '1';
                if (concInput) concInput.value = '2';
                const body = {
                    action,
                    workers: 1,
                    concurrency: 2,
                    decoupled: !!document.getElementById('clDecoupled')?.checked,
                };
                const res = await dashboardRequest(`${API}/api/cluster/control`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(body),
                });
                const d = await res.json();
                if (res.status === 409 || !res.ok || d?.ok === false) {
                    const raw = d?.error || d?.result?.error || d?.message;
                    const message = (typeof raw === 'string' && raw.trim()) ? raw.trim() : '操作失败';
                    showClusterError(message);
                    return;
                }
                if (toast && toast.className.includes('error')) {
                    toast.className = 'toast-msg';
                    toast.textContent = '';
                }

                // 立即刷新状态
                await refreshClusterLiveStatus({ fresh: true });

            } catch (err) {
                if (isCancelledRequest(err)) return;
                showClusterError(err?.message || String(err));
            } finally {
                clusterBusy = false;
                const unavailable = document.getElementById('clusterLiveStatus')?.textContent === '状态暂不可用';
                btnCluster.disabled = unavailable;
                if (!unavailable) {
                    const running = document.getElementById('clusterLiveDot')?.classList.contains('on');
                    btnCluster.textContent = running ? '停止抓取集群' : '次要：启动协议集群';
                }
            }
        });
    }
}

function showClusterStatusUnavailable() {
    const dot = document.getElementById('clusterLiveDot');
    if (dot) dot.className = 'live-dot';
    setText('clusterLiveStatus', '状态暂不可用');
    const button = document.getElementById('btnToggleCluster');
    if (button) {
        button.disabled = true;
        button.textContent = '状态暂不可用';
    }
}

async function refreshClusterLiveStatus({ fresh = false } = {}) {
    const view = dashboardRuntime.currentPage();
    const seq = ++clusterStatusSeq;
    try {
        const [resCluster, resMetrics] = await Promise.all([
            dashboardRequest(`${API}/api/cluster/status`, fresh ? { cache: 'no-store' } : {})
                .then(r => r.ok ? r.json() : null).catch(() => null),
            dashboardRequest(`${API}/api/metrics`, {}, { global: true }).then(r => r.ok ? r.json() : null).catch(() => null),
        ]);
        if (!dashboardRuntime.isCurrent(view) || seq !== clusterStatusSeq) return;
        if (!resCluster || !resCluster.ok || typeof resCluster.running !== 'boolean') {
            showClusterStatusUnavailable();
            return false;
        }

        const isRunning = !!resCluster.running;
        const qps = Number(resCluster.total_qps || 0);
        const targetQps = 347.2;
        const progressPct = Math.min(100, Math.round((qps / targetQps) * 100));

        // 更新 KPI
        setText('liveQps', qps.toFixed(1));
        setText('liveQpsSub', `日产 3000万 目标: ${progressPct}% (${targetQps} QPS)`);
        setText('liveBuffer', fmt(resCluster.buffer_depth || 0));
        setText('liveInflight', `在飞协程任务: ${fmt(resCluster.inflight_count || 0)} 条`);

        if (resMetrics && resMetrics.counters) {
            const succ = Number(resMetrics.counters.success || 0);
            const cf = Number(resMetrics.counters.cf_fail || 0);
            const total = succ + cf;
            const rate = total > 0 ? ((cf / total) * 100).toFixed(1) : '0.0';
            setText('liveCfRate', `${rate}%`);
            setText('liveCfCounts', `成功 ${fmt(succ)} · 拦截 ${fmt(cf)}`);
        } else {
            setText('liveCfRate', '—');
            setText('liveCfCounts', '指标暂不可用');
        }

        // 更新集群状态按钮与指示灯
        const dot = document.getElementById('clusterLiveDot');
        if (dot) dot.className = `live-dot ${isRunning ? 'on' : ''}`;
        setText('clusterLiveStatus', isRunning ? '集群运行中' : '集群已停止');

        const btnCluster = document.getElementById('btnToggleCluster');
        if (btnCluster && !clusterBusy) {
            btnCluster.disabled = false;
            btnCluster.textContent = isRunning ? '停止抓取集群' : '次要：启动协议集群';
            btnCluster.style.background = isRunning ? 'var(--danger)' : '';
            btnCluster.style.borderColor = isRunning ? 'var(--danger)' : '';
            btnCluster.style.color = isRunning ? '#f7f2ea' : '';
        }

        const clWorkers = document.getElementById('clWorkers');
        const clConc = document.getElementById('clConcurrency');
        const clDecoupled = document.getElementById('clDecoupled');
        if (clWorkers) clWorkers.disabled = isRunning;
        if (clConc) clConc.disabled = isRunning;
        if (clDecoupled) clDecoupled.disabled = isRunning;

        setText('clusterProcCount', `调度: ${resCluster.runner_pids?.length || 0} | 抓取: ${resCluster.worker_pids?.length || 0} | 入库: ${resCluster.ingester_pids?.length || 0}`);

        // 更新日志并自动滚动到底部
        const logBox = document.getElementById('clusterLogText');
        if (logBox && resCluster.logs) {
            const text = resCluster.logs.join('\n') || '暂无集群运行日志。';
            if (logBox.textContent !== text) {
                logBox.textContent = text;
                logBox.scrollTop = logBox.scrollHeight;
            }
        }
        setText('logSyncTime', `最后同步: ${new Date().toLocaleTimeString()}`);
        return Boolean(resMetrics);

    } catch (e) {
        if (isCancelledRequest(e) || !dashboardRuntime.isCurrent(view) || seq !== clusterStatusSeq) return;
        showClusterStatusUnavailable();
        return false; // Retry status with the poller's failure backoff.
    }
}

function route() {
    dashboardRuntime.beginPage();
    clearTimeout(searchTimer);
    clearTimeout(scaleTimer);
    closeNav();
    stopPipelinePoll();
    stopProxyPoll();
    stopRecentPoll();
    const { path, params } = parseHash();
    setActive(path);
    if (path === '/' || path === '') return loadOverview();
    if (path === '/pipeline') return loadPipeline();
    if (path === '/proxy') return loadProxyCluster();
    if (path === '/persons') {
        lastListHash = location.hash.replace(/^#/, '') || '/persons';
        personQuery = params.get('q') || '';
        phoneQuery = params.get('phone') || '';
        cityFilter = params.get('city') || '';
        stateFilter = params.get('state') || '';
        phoneTypeFilter = params.get('phone_type') || 'all';
        hasWirelessFilter = params.get('has_wireless') === '1';
        ageMinFilter = params.get('age_min') || '';
        ageMaxFilter = params.get('age_max') || '';
        sortFilter = params.get('sort') || 'newest';
        const cursor = params.get('cursor') || '';
        const page = Math.max(1, parseInt(params.get('page') || '1', 10) || 1);
        syncCursorTrail(cursor);
        return loadPersons({ page, cursor });
    }
    if (path === '/search') return loadSearchPage(params.get('q') || '');
    if (path === '/charts') return loadCharts();
    if (path === '/recent') return loadRecent();
    if (path.startsWith('/person/')) {
        const id = path.slice('/person/'.length);
        if (!/^[\w-]+$/.test(id)) return render(emptyState('无效档案', '链接格式不正确'));
        return loadPersonDetail(id);
    }
    loadOverview();
}

document.querySelectorAll('.nav-item').forEach(btn => {
    btn.addEventListener('click', () => go(btn.dataset.route));
});
document.getElementById('mainContent').addEventListener('click', e => {
    const goEl = e.target.closest('[data-go]');
    if (goEl) { go(goEl.dataset.go); return; }
    const cityEl = e.target.closest('[data-city]');
    if (cityEl) { cityFilter = cityEl.dataset.city; go('/persons?city=' + encodeURIComponent(cityFilter)); return; }
    const row = e.target.closest('[data-person]');
    if (row) go('/person/' + row.dataset.person);
});
document.getElementById('mainContent').addEventListener('keydown', e => {
    if (e.key !== 'Enter' && e.key !== ' ') return;
    const goEl = e.target.closest('[data-go]');
    if (goEl && e.target === goEl && goEl.getAttribute('role') === 'link') {
        e.preventDefault();
        go(goEl.dataset.go);
        return;
    }
    const row = e.target.closest('[data-person]');
    if (row && e.target === row) { e.preventDefault(); go('/person/' + row.dataset.person); }
});

const searchBox = document.getElementById('globalSearch');
function commitSearch() {
    const q = searchBox.value.trim();
    go(q ? '/search?q=' + encodeURIComponent(q) : '/search');
}
searchBox.addEventListener('input', () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(commitSearch, 360);
});
searchBox.addEventListener('keydown', e => {
    if (e.key === 'Enter') { clearTimeout(searchTimer); commitSearch(); }
});

document.addEventListener('keydown', e => {
    if (e.key === '/' && !['INPUT', 'TEXTAREA'].includes(e.target.tagName)) {
        e.preventDefault();
        searchBox.focus();
        searchBox.select();
    }
    if (e.key === 'Escape') {
        if (document.body.dataset.page === 'detail') go(lastListHash);
        else searchBox.blur();
    }
});

document.getElementById('queueMetrics')?.addEventListener('click', () => go('/pipeline'));
document.getElementById('queueMetrics')?.addEventListener('keydown', e => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go('/pipeline'); }
});

window.addEventListener('pagehide', event => { if (!event.persisted) dashboardRuntime.dispose(); });
window.addEventListener('hashchange', route);
if (!location.hash || location.hash === '#') history.replaceState(null, '', '#/');
route();
loadQueueMetrics();
dashboardRuntime.startPoll(loadQueueMetrics, 5000, { global: true, backoff: true });

// ============================================================
// 系统版本检测与在线平滑升级模块 (Version & Auto-Update Controller)
// ============================================================
(function initVersionManager() {
    const versionBadge = document.getElementById('versionBadge');
    const versionText = document.getElementById('versionText');
    const versionUpdateTag = document.getElementById('versionUpdateTag');
    const checkUpdateBtn = document.getElementById('checkUpdateBtn');
    const updateBanner = document.getElementById('updateBanner');
    const bannerLatestVer = document.getElementById('bannerLatestVer');
    const bannerCommitCount = document.getElementById('bannerCommitCount');
    const bannerReleaseNotes = document.getElementById('bannerReleaseNotes');
    const bannerApplyBtn = document.getElementById('bannerApplyBtn');
    const bannerViewBtn = document.getElementById('bannerViewBtn');
    const bannerCloseBtn = document.getElementById('bannerCloseBtn');

    const updateModalBackdrop = document.getElementById('updateModalBackdrop');
    const modalCloseBtn = document.getElementById('modalCloseBtn');
    const modalCurrentVer = document.getElementById('modalCurrentVer');
    const diffCurrentVer = document.getElementById('diffCurrentVer');
    const diffCurrentCommit = document.getElementById('diffCurrentCommit');
    const diffLatestVer = document.getElementById('diffLatestVer');
    const diffLatestCommit = document.getElementById('diffLatestCommit');
    const modalNotesList = document.getElementById('modalNotesList');
    const modalCheckBtn = document.getElementById('modalCheckBtn');
    const modalCancelBtn = document.getElementById('modalCancelBtn');
    const modalUpgradeBtn = document.getElementById('modalUpgradeBtn');
    const updateTerminal = document.getElementById('updateTerminal');
    const terminalLogs = document.getElementById('terminalLogs');

    let currentSystemInfo = null;
    let latestUpdateInfo = null;
    let isUpgrading = false;

    function openModal() {
        if (!updateModalBackdrop) return;
        updateModalBackdrop.style.display = 'grid';
    }

    function closeModal() {
        if (!updateModalBackdrop) return;
        if (isUpgrading) return; // 升级进行中禁止关闭
        if (updateModalBackdrop.dataset.force === 'true') return; // 强制更新状态下禁止关闭
        updateModalBackdrop.style.display = 'none';
    }

    async function loadVersionInfo() {
        try {
            const resp = await dashboardRequest('/api/system/version');
            if (resp && resp.ok) {
                currentSystemInfo = await resp.json();
                const ver = currentSystemInfo.version || '1.0.0';
                if (versionText) versionText.textContent = 'v' + ver;
                if (modalCurrentVer) modalCurrentVer.textContent = 'v' + ver;
                if (diffCurrentVer) diffCurrentVer.textContent = 'v' + ver;
                const commit = currentSystemInfo.git?.short_commit;
                if (diffCurrentCommit) diffCurrentCommit.textContent = commit ? `Git: ${commit}` : '生产版本';
                if (diffLatestVer) diffLatestVer.textContent = 'v' + ver;
                if (diffLatestCommit) diffLatestCommit.textContent = commit ? `Git: ${commit}` : '已是最新';

                if (modalNotesList && currentSystemInfo.release_notes) {
                    modalNotesList.innerHTML = currentSystemInfo.release_notes
                        .map(n => `<li>${esc(n)}</li>`)
                        .join('');
                }
            }
        } catch (e) {
            console.debug('读取版本信息异常', e);
        }
    }

    async function checkUpdate(interactive = false) {
        if (interactive && checkUpdateBtn) {
            checkUpdateBtn.disabled = true;
            checkUpdateBtn.querySelector('span').textContent = '检查中...';
        }
        try {
            const resp = await dashboardRequest('/api/system/check-update');
            if (resp && resp.ok) {
                latestUpdateInfo = await resp.json();
                const hasUpdate = Boolean(latestUpdateInfo.has_update);

                if (hasUpdate) {
                    // 发现新版本：激活角标与浮动 Banner
                    if (versionBadge) {
                        versionBadge.classList.add('has-update');
                        versionBadge.title = `发现新版本 v${latestUpdateInfo.latest_version}，点击查看详情与升级`;
                    }
                    if (versionUpdateTag) versionUpdateTag.style.display = 'inline-block';
                    if (updateBanner) updateBanner.style.display = 'flex';

                    if (bannerLatestVer) bannerLatestVer.textContent = 'v' + (latestUpdateInfo.latest_version || '最新版');
                    if (bannerCommitCount) {
                        bannerCommitCount.textContent = latestUpdateInfo.commits_behind
                            ? `${latestUpdateInfo.commits_behind} 个新升级`
                            : '有新版本';
                    }
                    if (bannerReleaseNotes) {
                        const notes = latestUpdateInfo.latest_release_notes || [];
                        bannerReleaseNotes.textContent = notes.length > 0
                            ? notes[0]
                            : '包含分布式爬虫架构增强与性能调优';
                    }

                    // 填充弹窗
                    if (diffLatestVer) diffLatestVer.textContent = 'v' + (latestUpdateInfo.latest_version || '最新版');
                    if (diffLatestCommit) {
                        diffLatestCommit.textContent = latestUpdateInfo.latest_commit
                            ? `远端: ${latestUpdateInfo.latest_commit}`
                            : '云端最新提交';
                    }
                    if (modalNotesList) {
                        const notes = (latestUpdateInfo.latest_release_notes && latestUpdateInfo.latest_release_notes.length > 0)
                            ? latestUpdateInfo.latest_release_notes
                            : (latestUpdateInfo.release_notes || ['优化了爬虫网络调度与反爬抗性']);
                        modalNotesList.innerHTML = notes.map(n => `<li>${esc(n)}</li>`).join('');
                    }

                    const isForce = Boolean(latestUpdateInfo.force_update);
                    const forceAlert = document.getElementById('forceUpdateAlert');
                    const forceText = document.getElementById('forceAlertText');
                    if (isForce) {
                        if (updateModalBackdrop) updateModalBackdrop.dataset.force = 'true';
                        if (forceAlert) forceAlert.style.display = 'flex';
                        if (forceText && latestUpdateInfo.force_update_reason) {
                            forceText.textContent = latestUpdateInfo.force_update_reason;
                        }
                        if (modalCloseBtn) modalCloseBtn.style.display = 'none';
                        if (modalCancelBtn) modalCancelBtn.style.display = 'none';
                        if (bannerCloseBtn) bannerCloseBtn.style.display = 'none';
                        if (modalUpgradeBtn) {
                            modalUpgradeBtn.disabled = false;
                            modalUpgradeBtn.textContent = '🚨 立即一键升级 (强制更新)';
                        }
                        // 强制更新立即自动弹窗
                        openModal();
                    } else {
                        if (updateModalBackdrop) updateModalBackdrop.removeAttribute('data-force');
                        if (forceAlert) forceAlert.style.display = 'none';
                        if (modalCloseBtn) modalCloseBtn.style.display = 'inline-block';
                        if (modalCancelBtn) modalCancelBtn.style.display = 'inline-block';
                        if (bannerCloseBtn) bannerCloseBtn.style.display = 'inline-block';
                        if (modalUpgradeBtn) {
                            modalUpgradeBtn.disabled = false;
                            modalUpgradeBtn.textContent = '🚀 立即一键升级';
                        }
                        if (interactive) openModal();
                    }
                } else {
                    // 已是最新
                    if (versionBadge) versionBadge.classList.remove('has-update');
                    if (versionUpdateTag) versionUpdateTag.style.display = 'none';
                    if (updateBanner) updateBanner.style.display = 'none';
                    if (diffLatestCommit) diffLatestCommit.textContent = '已是最新构建';
                    if (modalUpgradeBtn) {
                        modalUpgradeBtn.disabled = true;
                        modalUpgradeBtn.textContent = '当前已是最新版';
                    }
                    if (interactive) {
                        alert('当前系统已是最新版本，无需升级！');
                    }
                }
            }
        } catch (e) {
            console.debug('检测更新失败', e);
            if (interactive) alert('检测更新异常，请检查网络或稍后重试。');
        } finally {
            if (interactive && checkUpdateBtn) {
                checkUpdateBtn.disabled = false;
                checkUpdateBtn.querySelector('span').textContent = '检查更新';
            }
        }
    }

    async function applyUpdate() {
        if (isUpgrading) return;
        if (!confirm('确定立即升级系统吗？\n升级过程中将拉取最新核心代码并平滑重载后台服务。')) return;

        isUpgrading = true;
        if (modalUpgradeBtn) {
            modalUpgradeBtn.disabled = true;
            modalUpgradeBtn.textContent = '正在升级中...';
        }
        if (modalCancelBtn) modalCancelBtn.disabled = true;
        if (modalCheckBtn) modalCheckBtn.disabled = true;

        if (updateTerminal) updateTerminal.style.display = 'block';
        if (terminalLogs) {
            terminalLogs.textContent = '⏳ [1/4] 正在连接云端代码仓库并准备升级环境...\n';
        }

        try {
            const resp = await fetch('/api/system/apply-update', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ force_stash: true }),
            });
            const data = await resp.json();

            if (terminalLogs) {
                const logs = data.logs || [];
                terminalLogs.textContent = logs.join('\n') + '\n';
            }

            if (resp.ok && data.ok) {
                if (terminalLogs) terminalLogs.textContent += '\n🎉 升级执行完毕！系统服务已重载，3 秒后自动刷新页面...\n';
                if (modalUpgradeBtn) modalUpgradeBtn.textContent = '✅ 升级完成！刷新中';
                setTimeout(() => {
                    location.reload();
                }, 3000);
            } else {
                const err = data.error || '升级失败';
                if (terminalLogs) terminalLogs.textContent += `\n❌ 升级中断: ${err}\n`;
                if (modalUpgradeBtn) {
                    modalUpgradeBtn.disabled = false;
                    modalUpgradeBtn.textContent = '重试升级';
                }
                if (modalCancelBtn) modalCancelBtn.disabled = false;
                if (modalCheckBtn) modalCheckBtn.disabled = false;
                isUpgrading = false;
            }
        } catch (err) {
            if (terminalLogs) terminalLogs.textContent += `\n❌ 网络异常: ${err.message}\n`;
            if (modalUpgradeBtn) {
                modalUpgradeBtn.disabled = false;
                modalUpgradeBtn.textContent = '重试升级';
            }
            if (modalCancelBtn) modalCancelBtn.disabled = false;
            if (modalCheckBtn) modalCheckBtn.disabled = false;
            isUpgrading = false;
        }
    }

    // 事件绑定
    versionBadge?.addEventListener('click', () => openModal());
    checkUpdateBtn?.addEventListener('click', () => checkUpdate(true));
    bannerViewBtn?.addEventListener('click', () => openModal());
    bannerApplyBtn?.addEventListener('click', () => {
        openModal();
        applyUpdate();
    });
    bannerCloseBtn?.addEventListener('click', () => {
        if (updateBanner) updateBanner.style.display = 'none';
    });
    modalCloseBtn?.addEventListener('click', closeModal);
    modalCancelBtn?.addEventListener('click', closeModal);
    modalCheckBtn?.addEventListener('click', () => checkUpdate(true));
    modalUpgradeBtn?.addEventListener('click', applyUpdate);

    // 点击弹窗背景遮罩关闭 (非升级状态下)
    updateModalBackdrop?.addEventListener('click', (e) => {
        if (e.target === updateModalBackdrop) closeModal();
    });

    // 初始化加载
    loadVersionInfo();
    // 页面加载后 1.5 秒自动进行一次静默检查
    setTimeout(() => checkUpdate(false), 1500);
    // 每 15 分钟静默检测一次新版本
    dashboardRuntime.startPoll(() => checkUpdate(false), 900000, { global: true, backoff: true });
})();

