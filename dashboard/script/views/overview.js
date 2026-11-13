import { api } from '../api/client.js';
import { h, icon, fmtCompact, fmtInt, fmtAgo, fmtDuration, axisTime, rangeById, SEVERITY, STATUS } from '../ui/dom.js';
import { chartCard, panel, statTile, statusChip, emptyState } from '../ui/components.js';
import { healthStrip } from '../ui/charts.js';

export const title = 'Overview';

export function controls() {
    return [];
}

export async function load(ctx, signal) {
    const { app, source, range } = ctx.state;
    const [overview, sources, alerts] = await Promise.all([
        api.overview({ app, source, range }, { signal }),
        api.sources({ app, source, range }, { signal }),
        api.alerts({ app, source, state: 'firing', limit: 5 }, { signal }),
    ]);
    return { overview, sources, alerts };
}

const RANK = { down: 0, degraded: 1, ok: 2 };

export function render({ overview, sources, alerts }, ctx) {
    const { range, app, source } = ctx.state;
    const tick = axisTime(range);
    const scope = app === 'all' ? 'app' : source === 'all' ? 'source' : null;

    const kpis = h('section', { class: 'kpis', 'aria-label': 'Key metrics' }, overview.kpis.map((k) => statTile(k, range)));

    const traffic = chartCard({
        class: 'span-8',
        title: 'Traffic',
        subtitle: scope ? `Events per minute, by ${scope}` : 'Events per minute',
        data: overview.traffic,
        options: { yFormat: fmtCompact, axisTime: tick, tipFormat: (v) => `${fmtCompact(v)} events/min`, area: true, height: 290 },
    });

    const firing = panel({
        class: 'span-4',
        title: 'Firing alerts',
        subtitle: 'Needs attention now',
        actions: h('a', { class: 'btn btn-sm', href: ctx.hrefFor({ view: 'alerts', params: {} }) }, 'View all'),
    }, alerts.items.length
        ? h('div', { class: 'mini-alerts' }, alerts.items.map((a) => h('a', {
            class: `mini-alert chip-${a.severity}`,
            href: ctx.hrefFor({ view: 'alerts', params: {} }),
        },
        icon(SEVERITY[a.severity].icon, 16),
        h('div', null,
            h('strong', null, a.title),
            h('span', { class: 'sub' }, [
                SEVERITY[a.severity]?.label ?? a.severity, app === 'all' ? `${a.app}/${a.source}` : a.source,
                a.group ? `${a.group.key} ${a.group.value}` : null, `last seen ${fmtAgo(a.lastSeenAt)}`,
            ].filter(Boolean).join(', '))))))
        : emptyState({ title: 'All clear', message: 'No alerts are firing for this selection.' }));

    const raised = chartCard({
        class: 'span-6',
        title: 'Alerts raised',
        subtitle: 'New alerts in each interval, by severity',
        kind: 'bars',
        data: overview.alerts,
        options: { yFormat: fmtCompact, tipFormat: fmtInt, axisTime: tick },
    });

    const mix = chartCard({
        class: 'span-6',
        title: 'Event types',
        subtitle: 'Events per minute, by event type',
        data: overview.eventMix,
        options: { yFormat: fmtCompact, axisTime: tick, tipFormat: (v) => `${fmtCompact(v)} events/min` },
    });

    return h('div', { class: 'view-stack', style: { display: 'contents' } },
        kpis,
        h('div', { class: 'grid' }, traffic, firing, raised, mix, healthPanel(sources, range, ctx)));
}

function healthPanel(sources, range, ctx) {
    const sorted = [...sources].sort((a, b) => RANK[a.status] - RANK[b.status] || a.app.localeCompare(b.app) || a.name.localeCompare(b.name));
    const shown = sorted.slice(0, 8);
    const cellMs = rangeById(range).ms / (sources[0]?.health.length || 48);

    const legend = h('div', { class: 'health-legend' },
        ['ok', 'degraded', 'down'].map((st) => h('span', { class: 'legend-item' },
            h('span', { class: `pixel pixel-${st}` }), STATUS[st].label)),
        h('span', null, `Each block is ${fmtDuration(cellMs)}`));

    return panel({
        class: 'span-12',
        title: 'Source health',
        subtitle: 'Worst alert state in each block, oldest on the left',
        actions: h('a', { class: 'btn btn-sm', href: ctx.hrefFor({ view: 'sources', params: {} }) },
            sorted.length > shown.length ? `All ${sorted.length} sources` : 'Source details', icon('arrow', 14)),
    },
    legend,
    shown.length ? h('div', { class: 'health-rows' }, shown.map((s) => h('div', { class: 'health-row' },
        h('div', { class: 'health-name' }, h('strong', null, s.name), h('span', { class: 'sub' }, s.app)),
        healthStrip(s.health, { name: s.name }),
        statusChip(s.status)))) : emptyState({ title: 'No sources yet', message: 'Sources appear once an application XML declares them or events arrive.' }));
}
