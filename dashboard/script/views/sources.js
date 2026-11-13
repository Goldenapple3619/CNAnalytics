import { api } from '../api/client.js';
import { h, icon, fmtCompact, fmtAgo, SEVERITY_ORDER } from '../ui/dom.js';
import { panel, statusChip, severityChip, selectControl, searchInput, emptyState } from '../ui/components.js';
import { healthStrip, sparkline } from '../ui/charts.js';

export const title = 'Sources';

const STATUS_RANK = { down: 2, degraded: 1, ok: 0 };

const COLUMNS = [
    { key: 'name', label: 'Source', get: (s) => s.name },
    { key: 'status', label: 'Status', get: (s) => STATUS_RANK[s.status] },
    { key: 'events', label: 'Events', num: true, get: (s) => s.events },
    { key: 'perMinute', label: 'Events per minute', num: true, get: (s) => s.perMinute },
    { key: 'alerts', label: 'Firing alerts', num: true, get: (s) => s.firingAlerts },
    { key: 'lastEventAt', label: 'Last event', num: true, get: (s) => s.lastEventAt ?? 0 },
    { key: 'health', label: 'Health', get: null },
];

// Client-side filter and sort state. Survives refreshes, reset when you leave the view.
let local = { q: '', status: 'all', sort: { key: 'status', dir: 'desc' } };

export function reset() {
    local = { q: '', status: 'all', sort: { key: 'status', dir: 'desc' } };
}

export function controls(ctx) {
    return [
        searchInput({ placeholder: 'Search sources', value: local.q, onInput: (v) => { local.q = v; ctx.rerender(); } }),
        selectControl({
            label: 'Status',
            value: local.status,
            options: [{ id: 'all', label: 'All' }, { id: 'ok', label: 'Healthy' }, { id: 'degraded', label: 'Warning' }, { id: 'down', label: 'Critical' }],
            onChange: (v) => { local.status = v; ctx.rerender(); },
        }),
    ];
}

export async function load(ctx, signal) {
    const { app, source, range } = ctx.state;
    return api.sources({ app, source, range }, { signal });
}

export function render(sources, ctx) {
    const q = local.q.trim().toLowerCase();
    const rows = sources
        .filter((s) => (local.status === 'all' || s.status === local.status))
        .filter((s) => !q || s.name.toLowerCase().includes(q) || s.app.toLowerCase().includes(q));

    const col = COLUMNS.find((c) => c.key === local.sort.key);
    const dir = local.sort.dir === 'asc' ? 1 : -1;
    rows.sort((a, b) => {
        const av = col.get(a), bv = col.get(b);
        const cmp = typeof av === 'string' ? av.localeCompare(bv) : av - bv;
        return cmp * dir || b.events - a.events;
    });

    const sortBy = (key) => {
        local.sort = local.sort.key === key
            ? { key, dir: local.sort.dir === 'asc' ? 'desc' : 'asc' }
            : { key, dir: key === 'name' ? 'asc' : 'desc' };
        ctx.rerender();
    };

    const head = h('tr', null, COLUMNS.map((c) => {
        const sorted = local.sort.key === c.key;
        return h('th', {
            class: c.num ? 'num' : '',
            'aria-sort': sorted ? (local.sort.dir === 'asc' ? 'ascending' : 'descending') : null,
        }, c.get
            ? h('button', { class: 'sort-btn', type: 'button', onclick: () => sortBy(c.key) },
                c.label, icon(sorted && local.sort.dir === 'asc' ? 'up' : 'down', 13))
            : c.label);
    }).concat(h('th', null, h('span', { class: 'sr-only' }, 'Actions'))));

    const body = rows.map((s) => h('tr', { class: 'row' },
        h('td', null, h('div', { class: 'cell-main' }, h('strong', null, s.name), h('span', { class: 'sub' }, s.app))),
        h('td', null, statusChip(s.status)),
        h('td', { class: 'num' }, fmtCompact(s.events)),
        h('td', { class: 'num' }, h('div', { style: { display: 'flex', alignItems: 'center', justifyContent: 'flex-end', gap: '12px' } },
            fmtCompact(s.perMinute), sparkline(s.spark, { w: 72, h: 24 }))),
        h('td', { class: 'num' }, s.firingAlerts
            ? h('div', { style: { display: 'flex', alignItems: 'center', justifyContent: 'flex-end', gap: '8px' } },
                severityChip(s.worstSeverity ?? SEVERITY_ORDER[0]), String(s.firingAlerts))
            : '-'),
        h('td', { class: 'num' }, s.lastEventAt ? fmtAgo(s.lastEventAt) : '-'),
        h('td', { style: { minWidth: '220px', width: '26%' } }, healthStrip(s.health, { name: s.name })),
        h('td', null, h('a', { class: 'btn btn-sm', href: ctx.hrefFor({ view: 'events', app: s.app, source: s.id, params: {} }) }, 'Events'))));

    return panel({
        class: 'span-12',
        title: `${rows.length} of ${sources.length} sources`,
        subtitle: 'Select a column heading to sort',
    },
    rows.length
        ? h('div', { class: 'table-wrap' }, h('table', { class: 'data' }, h('thead', null, head), h('tbody', null, body)))
        : emptyState({
            title: 'No sources match',
            message: 'Try a different search or status.',
            action: { label: 'Clear filters', onClick: () => { reset(); ctx.rebuildControls(); ctx.rerender(); } },
        }));
}
