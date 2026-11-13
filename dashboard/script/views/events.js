import { api } from '../api/client.js';
import { h, icon, fmtCompact, fmtInt, fmtPct, fmtAgo, fmtDateTime, axisTime } from '../ui/dom.js';
import { chartCard, panel, statTile, selectControl, searchInput, emptyState } from '../ui/components.js';
import { hBars, sparkline } from '../ui/charts.js';

export const title = 'Events';

let local = { q: '' };
const expanded = new Set();

export function reset() {
    local = { q: '' };
    expanded.clear();
}

export function controls(ctx) {
    const { app, source, params } = ctx.state;
    const types = [...new Set(ctx.state.apps
        .filter((a) => app === 'all' || a.id === app)
        .flatMap((a) => a.sources.filter((s) => source === 'all' || s.id === source))
        .flatMap((s) => s.eventTypes))].sort();

    return [
        selectControl({
            label: 'Event type',
            value: params.type || 'all',
            options: [{ id: 'all', label: 'All event types' }, ...types.map((t) => ({ id: t, label: t }))],
            onChange: (v) => ctx.setParams({ type: v === 'all' ? '' : v }),
        }),
    ];
}

export async function load(ctx, signal) {
    const { app, source, range, params } = ctx.state;
    return api.events({ app, source, range, type: params.type }, { signal });
}

export function render(data, ctx) {
    const { range } = ctx.state;

    const timeline = chartCard({
        class: 'span-8',
        title: 'Events over time',
        subtitle: 'Events in each interval, by event type',
        kind: 'bars',
        data: data.timeline,
        options: { yFormat: fmtCompact, tipFormat: fmtInt, axisTime: axisTime(range) },
    });

    const bySource = panel({
        class: 'span-4',
        title: 'Top sources',
        subtitle: 'Where events come from',
    }, data.bySource.length
        ? hBars(data.bySource.map((s) => ({ name: s.name, value: s.count, sub: s.app })), { format: fmtCompact })
        : emptyState({ title: 'No events', message: 'Nothing was received in this period.' }));

    return h('div', { style: { display: 'contents' } },
        h('section', { class: 'kpis', 'aria-label': 'Event totals' }, data.kpis.map((k) => statTile(k, range))),
        h('div', { class: 'grid' }, timeline, bySource, groupsPanel(data.groups, ctx)));
}

function groupsPanel(groups, ctx) {
    const list = h('div');
    const countLabel = h('span', { class: 'panel-sub' });

    const paint = () => {
        const q = local.q.trim().toLowerCase();
        const rows = groups.filter((g) => !q || [g.name, g.source, g.app].some((t) => t.toLowerCase().includes(q)));
        countLabel.textContent = `${rows.length} of ${groups.length} event types`;

        if (!rows.length) {
            list.replaceChildren(emptyState({
                title: groups.length ? 'No event types match' : 'No events in this period',
                message: groups.length ? 'Try a different search.' : 'Widen the time range or change the filters above.',
                action: groups.length ? { label: 'Clear search', onClick: () => { local.q = ''; input.value = ''; paint(); } } : null,
            }));
            return;
        }

        const body = [];
        for (const g of rows) {
            const open = expanded.has(g.id);
            body.push(h('tr', { class: 'row' },
                h('td', null, h('button', {
                    class: 'row-toggle', type: 'button', 'aria-expanded': String(open),
                    onclick: () => { open ? expanded.delete(g.id) : expanded.add(g.id); paint(); },
                },
                icon('chevron', 14),
                h('div', { class: 'cell-main' },
                    h('span', { class: 'row-title' }, g.name),
                    h('span', { class: 'sub' }, `${g.source}, ${g.app}`)))),
                h('td', { class: 'num' }, fmtInt(g.count)),
                h('td', null, sparkline(g.trend, { w: 84, h: 26 })),
                h('td', null, fmtAgo(g.firstSeen)),
                h('td', null, fmtAgo(g.lastSeen))));

            if (open) {
                body.push(h('tr', { class: 'detail' }, h('td', { colspan: 5 }, sampleDetail(g))));
            }
        }

        list.replaceChildren(h('div', { class: 'table-wrap' }, h('table', { class: 'data' },
            h('thead', null, h('tr', null,
                h('th', null, 'Event type'), h('th', { class: 'num' }, 'Count'), h('th', null, 'Trend'), h('th', null, 'First seen'), h('th', null, 'Last seen'))),
            h('tbody', null, body))));
    };

    const input = searchInput({ placeholder: 'Search event types', value: local.q, onInput: (v) => { local.q = v; paint(); } });
    const card = panel({ class: 'span-12', title: 'Event types', subtitle: 'One row per event type and source, most frequent first. Open a row to see the latest event.', actions: [countLabel, input] }, list);
    paint();
    return card;
}

/** Latest event of a group: its identifiers, the dimensions kept by the XML and the enrichment results. */
function sampleDetail(g) {
    const e = g.sample;
    const dims = Object.entries(e.dims || {});
    const hasEnriched = Object.keys(e.enriched || {}).length > 0;
    return h('div', null,
        h('dl', null,
            h('dt', null, 'Latest event'), h('dd', { class: 'code' }, e.eventId ?? '-'),
            h('dt', null, 'User'), h('dd', { class: 'code' }, e.userId ?? '-'),
            h('dt', null, 'Session'), h('dd', { class: 'code' }, e.sessionId ?? '-'),
            h('dt', null, 'Time'), h('dd', null, e.timestamp ? fmtDateTime(e.timestamp) : '-'),
            ...dims.flatMap(([k, v]) => [h('dt', null, k), h('dd', { class: 'code' }, typeof v === 'object' ? JSON.stringify(v) : String(v))]),
            h('dt', null, 'First seen'), h('dd', null, fmtDateTime(g.firstSeen)),
            h('dt', null, 'Last seen'), h('dd', null, fmtDateTime(g.lastSeen)),
            e.errors?.length ? [h('dt', null, 'Processing errors'), h('dd', null, e.errors.join('; '))] : null),
        hasEnriched ? h('pre', { class: 'stack-trace' }, JSON.stringify(e.enriched, null, 2)) : null);
}
