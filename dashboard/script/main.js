/**
 * App shell: routing, sidebar, filters and refresh. Views live in js/views and
 * only need to export controls(), load() and render() (see overview.js).
 *
 * Routes are hash based and carry the selection, so any state can be linked:
 *   #/events?app=moonreward&source=api&range=7d&type=reward_claim
 *
 * The sidebar lists applications and, under each one, its sources. `app` and `source`
 * are global filters shared by every view; a source only makes sense inside an app.
 */
import { config } from './config.js';
import { api } from './api/client.js';
import { h, icon, RANGES, STATUS, fmtAgo } from './ui/dom.js';
import { segmented, errorState } from './ui/components.js';
import hideTipOnScroll from './ui/scroll.js';
import * as overview from './views/overview.js';
import * as sources from './views/sources.js';
import * as events from './views/events.js';
import * as alerts from './views/alerts.js';

const VIEWS = { overview, sources, events, alerts };
const TABS = [
    { id: 'overview', label: 'Overview' },
    { id: 'sources', label: 'Sources' },
    { id: 'events', label: 'Events' },
    { id: 'alerts', label: 'Alerts' },
];
const DEFAULTS = { view: 'overview', app: 'all', source: 'all', range: '24h' };

const state = { ...DEFAULTS, params: {}, apps: [], live: true, updatedAt: null };
let renderToken = 0;
let abort = null;
let lastData = null; // { view, data }
let renderedView = null;
let fromControl = false;
let loading = false;

try {
    state.live = localStorage.getItem('dashboard.live') !== '0';
} catch { /* storage unavailable */ }

/* -------------------------------------------------------------------- routing */

function parseHash() {
    const raw = location.hash.replace(/^#\/?/, '');
    const [path, query = ''] = raw.split('?');
    const p = new URLSearchParams(query);
    const params = {};
    for (const [k, v] of p) if (k !== 'app' && k !== 'source' && k !== 'range') params[k] = v;
    const app = p.get('app') || DEFAULTS.app;
    return {
        view: VIEWS[path] ? path : DEFAULTS.view,
        app,
        source: app === DEFAULTS.app ? DEFAULTS.source : p.get('source') || DEFAULTS.source,
        range: RANGES.some((r) => r.id === p.get('range')) ? p.get('range') : DEFAULTS.range,
        params,
    };
}

function buildHash(s) {
    const p = new URLSearchParams();
    if (s.app !== DEFAULTS.app) p.set('app', s.app);
    if (s.app !== DEFAULTS.app && s.source !== DEFAULTS.source) p.set('source', s.source);
    if (s.range !== DEFAULTS.range) p.set('range', s.range);
    for (const [k, v] of Object.entries(s.params)) if (v) p.set(k, v);
    const q = p.toString();
    return `#/${s.view}${q ? `?${q}` : ''}`;
}

const merged = (patch) => {
    const next = { ...state, ...patch, params: patch.params ?? state.params };
    if (patch.app !== undefined && patch.app !== state.app && patch.source === undefined) next.source = DEFAULTS.source;
    return next;
};
const hrefFor = (patch) => buildHash(merged(patch));

function navigate(patch, { control = false } = {}) {
    const hash = buildHash(merged(patch));
    if (hash === (location.hash || buildHash({ ...DEFAULTS, params: {} }))) return;
    fromControl = control;
    location.hash = hash;
}

const ctx = {
    state,
    hrefFor,
    navigate,
    refresh: () => refresh(),
    setParams(patch, { rebuild = false } = {}) {
        const params = { ...state.params, ...patch };
        for (const k of Object.keys(params)) if (!params[k]) delete params[k];
        navigate({ params }, { control: !rebuild });
    },
    rerender() {
        if (lastData?.view === state.view) body.replaceChildren(VIEWS[state.view].render(lastData.data, ctx));
    },
    rebuildControls: () => paintControls(),
};

/* --------------------------------------------------------------------- shell */

const sidebar = document.getElementById('sidebar');
const content = document.getElementById('content');
const topbar = document.getElementById('topbar-actions');

const titleEl = h('h2', { class: 'page-title' });
const subEl = h('p', { class: 'page-sub' });
const tabsEl = h('nav', { class: 'tabs', 'aria-label': 'Views' });
const filtersLeft = h('div', { class: 'filters-left' });
const updatedEl = h('span', { class: 'updated' });
const body = h('div', { class: 'view' });

const liveBtn = h('button', {
    class: 'btn', type: 'button', 'aria-pressed': String(state.live), title: `Refresh every ${config.refreshSeconds} seconds`,
    onclick: () => {
        state.live = !state.live;
        liveBtn.setAttribute('aria-pressed', String(state.live));
        try { localStorage.setItem('dashboard.live', state.live ? '1' : '0'); } catch { /* ignore */ }
        if (state.live) refresh();
    },
}, h('span', { class: 'live-dot' }), 'Live');

const refreshBtn = h('button', { class: 'btn', type: 'button', onclick: () => refresh() }, icon('refresh', 15), 'Refresh');

content.append(h('div', { class: 'page' },
    h('div', { class: 'page-head' }, h('div', null, titleEl, subEl)),
    tabsEl,
    h('div', { class: 'filters' }, filtersLeft, h('span', { class: 'grow' }), h('div', { class: 'filters-right' }, updatedEl, liveBtn, refreshBtn)),
    body));

/* -------------------------------------------------------------------- painting */

const worst = (list) => (list.includes('down') ? 'down' : list.includes('degraded') ? 'degraded' : 'ok');
const totalFiring = () => state.apps.reduce((n, a) => n + a.firingAlerts, 0);

function paintSidebar() {
    const entry = ({ name, status, firing, current, onClick, cls = '', detail }) => h('button', {
        class: `application-name ${cls}`.trim(), type: 'button', 'aria-current': String(current), onclick: onClick,
    },
    h('span', { class: `pixel pixel-${status}` }),
    h('span', { class: 'app-label' }, h('span', { class: 'app-name' }, name),
        detail === null ? null : h('span', { class: 'app-state' }, detail ?? STATUS[status]?.label ?? 'No data')),
    firing ? h('span', { class: 'count-badge', title: `${firing} firing` }, String(firing)) : null);

    const all = state.app === 'all';
    sidebar.replaceChildren(
        h('p', { class: 'sidebar-title' }, 'Applications'),
        entry({
            name: 'All apps', status: state.apps.length ? worst(state.apps.map((a) => a.status)) : 'nodata',
            firing: totalFiring(), current: all, onClick: () => navigate({ app: 'all', source: 'all', params: {} }),
        }),
        ...state.apps.map((a) => h('div', { class: 'app-group' },
            entry({
                name: a.name, status: a.status, firing: a.firingAlerts, current: state.app === a.id && state.source === 'all',
                onClick: () => navigate({ app: a.id, source: 'all', params: {} }),
            }),
            a.sources.length
                ? h('div', { class: 'source-list', role: 'group', 'aria-label': `Sources of ${a.name}` }, a.sources.map((src) => entry({
                    name: src.name, status: src.status, firing: src.firingAlerts, cls: 'source-name', detail: null,
                    current: state.app === a.id && state.source === src.id,
                    onClick: () => navigate({ app: a.id, source: src.id, params: {} }),
                })))
                : null)));
}

function paintTopbar() {
    const n = totalFiring();
    topbar.replaceChildren(h('a', {
        class: `pill-btn${n ? ' has-alerts' : ''}`, href: hrefFor({ view: 'alerts', app: 'all', params: {} }),
    }, icon('bell', 16), n ? `${n} alerts firing` : 'No alerts firing'));
}

function paintChrome() {
    const app = state.apps.find((a) => a.id === state.app);
    const src = app?.sources.find((x) => x.id === state.source);
    const scoped = state.apps.filter((a) => state.app === 'all' || a.id === state.app);
    const srcCount = scoped.reduce((n, a) => n + a.sources.length, 0);
    const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;

    titleEl.textContent = state.app === 'all' ? 'All apps' : src ? `${app.name} / ${src.name}` : app?.name || state.app;
    subEl.textContent = !state.apps.length ? ''
        : src ? `Source of ${app.name}, ${plural(src.eventTypes.length, 'event type')}`
            : app ? `${plural(srcCount, 'source')}, ${plural(new Set(app.sources.flatMap((x) => x.eventTypes)).size, 'event type')}`
                : `${plural(scoped.length, 'app')}, ${plural(srcCount, 'source')}`;

    const firing = src ? src.firingAlerts : scoped.reduce((n, a) => n + a.firingAlerts, 0);
    tabsEl.replaceChildren(...TABS.map((t) => h('a', {
        class: 'tab', href: hrefFor({ view: t.id, params: {} }), 'aria-current': t.id === state.view ? 'page' : null,
    }, t.label, t.id === 'alerts' && firing ? h('span', { class: 'count-badge' }, String(firing)) : null)));

    document.title = `${VIEWS[state.view].title} - Dashboard`;
}

function paintControls() {
    const view = VIEWS[state.view];
    filtersLeft.replaceChildren(
        ...[
            view.usesRange === false ? null : segmented({
                label: 'Time range',
                options: RANGES.map((r) => ({ id: r.id, label: r.label })),
                value: state.range,
                onChange: (id) => navigate({ range: id }, { control: true }),
            }),
            ...view.controls(ctx),
        ].filter(Boolean));
}

function paintUpdated() {
    updatedEl.textContent = state.updatedAt ? `Updated ${fmtAgo(state.updatedAt)}` : '';
}

/* ------------------------------------------------------------------- data flow */

async function loadApps() {
    try {
        state.apps = await api.apps();
    } catch { /* the view's own request reports the problem */ }
    paintSidebar();
    paintTopbar();
}

async function refresh() {
    const token = ++renderToken;
    abort?.abort();
    abort = new AbortController();
    const view = VIEWS[state.view];

    loading = true;
    body.classList.add('is-loading');
    refreshBtn.querySelector('svg').classList.add('spin');
    refreshBtn.disabled = true;

    try {
        const [data] = await Promise.all([view.load(ctx, abort.signal), loadApps()]);
        if (token !== renderToken) return;
        lastData = { view: state.view, data };
        paintChrome();
        body.replaceChildren(view.render(data, ctx));
        renderedView = state.view;
        state.updatedAt = Date.now();
        paintUpdated();
    } catch (err) {
        if (err?.name === 'AbortError' || token !== renderToken) return;
        lastData = null;
        body.replaceChildren(errorState(err, () => refresh()));
        renderedView = state.view;
    } finally {
        if (token === renderToken) {
            loading = false;
            body.classList.remove('is-loading');
            refreshBtn.querySelector('svg').classList.remove('spin');
            refreshBtn.disabled = false;
        }
    }
}

async function route() {
    const next = parseHash();
    const wasControl = fromControl;
    fromControl = false;

    if (next.view !== state.view) VIEWS[state.view].reset?.();
    Object.assign(state, next);

    paintSidebar();
    paintChrome();
    if (!wasControl) paintControls();
    if (renderedView !== state.view) {
        body.replaceChildren(h('p', { class: 'loading-note' }, 'Loading...'));
        renderedView = null;
    }
    await refresh();
}

/* ----------------------------------------------------------------------- boot */

window.addEventListener('hashchange', route);
hideTipOnScroll(content);

setInterval(paintUpdated, 1000);
setInterval(() => {
    if (state.live && !document.hidden && !loading) refresh();
}, config.refreshSeconds * 1000);

loadApps().then(route);
