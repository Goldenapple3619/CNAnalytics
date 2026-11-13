import { api } from '../api/client.js';
import { h, icon, fmtAgo, fmtDuration, fmtCompact, SEVERITY, SEVERITY_ORDER } from '../ui/dom.js';
import { panel, severityChip, stateChip, segmented, selectControl, emptyState, toast } from '../ui/components.js';

export const title = 'Alerts';

// Alerts are about now, so the time range control is hidden on this view.
export const usesRange = false;

const STATES = [
    { id: 'firing', label: 'Firing' },
    { id: 'acknowledged', label: 'Acknowledged' },
    { id: 'resolved', label: 'Resolved' },
    { id: 'all', label: 'All' },
];

const SEVERITIES = [
    { id: 'all', label: 'All severities' },
    ...SEVERITY_ORDER.map((id) => ({ id, label: SEVERITY[id].label })),
];

export function controls(ctx) {
    const { params } = ctx.state;
    return [
        segmented({
            label: 'Alert state',
            options: STATES,
            value: params.state || 'firing',
            onChange: (v) => ctx.setParams({ state: v === 'firing' ? '' : v }),
        }),
        selectControl({
            label: 'Severity',
            value: params.severity || 'all',
            options: SEVERITIES,
            onChange: (v) => ctx.setParams({ severity: v === 'all' ? '' : v }),
        }),
    ];
}

export async function load(ctx, signal) {
    const { app, source, params } = ctx.state;
    return api.alerts({ app, source, state: params.state || 'firing', severity: params.severity }, { signal });
}

/** The number that tripped the rule, when the rule has one (a rate or distinct count); null otherwise. */
function reading(m) {
    if (!m) return null;
    return { value: fmtCompact(m.value), threshold: m.threshold == null ? null : fmtCompact(m.threshold) };
}

export function render(data, ctx) {
    const tiles = h('section', { class: 'alert-counts', 'aria-label': 'Firing alerts by severity' },
        SEVERITY_ORDER.map((sev) => h('div', { class: 'count-tile' },
            h('span', { class: `chip chip-${sev}`, style: { padding: '6px' } }, icon(SEVERITY[sev].icon, 20)),
            h('div', { class: 'what' },
                h('span', { class: 'n' }, String(data.counts[sev] ?? 0)),
                h('span', { class: 'sub' }, `${SEVERITY[sev].label} firing`)))));

    const state = ctx.state.params.state || 'firing';
    const list = data.items.length
        ? h('div', { class: 'alert-list' }, data.items.map((a) => alertRow(a, ctx)))
        : panel({}, emptyState({
            title: state === 'firing' ? 'Nothing is firing' : 'No alerts match',
            message: state === 'firing'
                ? 'Alerts that fire will show up here, with a way to acknowledge them.'
                : 'Try another state or severity.',
            action: state === 'firing' ? null : { label: 'Show firing alerts', onClick: () => ctx.setParams({ state: '', severity: '' }, { rebuild: true }) },
        }));

    return h('div', { style: { display: 'contents' } }, tiles, list);
}

function alertRow(a, ctx) {
    const r = reading(a.metric);

    const status = a.state === 'firing'
        ? `Firing for ${fmtDuration(Date.now() - a.firedAt)}, last seen ${fmtAgo(a.lastSeenAt)}`
        : a.state === 'acknowledged'
            ? `Acknowledged${a.acknowledgedAt ? ` ${fmtAgo(a.acknowledgedAt)}` : ''}`
            : `Resolved${a.resolvedAt ? ` ${fmtAgo(a.resolvedAt)}` : ''}`;

    const action = (label, call, done, cls) => {
        const btn = h('button', { class: `btn ${cls} btn-sm`, type: 'button' }, label);
        btn.addEventListener('click', async () => {
            btn.disabled = true;
            try {
                await call(a.id);
                toast(`${done}: ${a.title}`);
                ctx.refresh();
            } catch (err) {
                btn.disabled = false;
                toast(err.message, { error: true });
            }
        });
        return btn;
    };

    const eventsHref = ctx.hrefFor({ view: 'events', app: a.app, source: a.source, params: { type: a.eventType } });
    return h('article', { class: `alert${a.state === 'resolved' ? ' is-resolved' : ''}` },
        h('div', null,
            h('div', { class: 'alert-title' }, severityChip(a.severity), h('h3', null, a.title), stateChip(a.state)),
            h('p', null, a.description),
            h('div', { class: 'meta' },
                h('a', { class: 'link', href: eventsHref }, a.source),
                h('span', null, a.app),
                a.group ? h('span', null, `${a.group.key} = ${a.group.value}`) : null,
                h('span', null, `${fmtCompact(a.occurrences)} ${a.occurrences === 1 ? 'occurrence' : 'occurrences'}`),
                h('span', null, status))),
        h('div', { class: 'alert-side' },
            r ? h('div', { class: 'reading' },
                h('strong', null, r.value),
                h('span', { class: 'sub' }, `${a.metric.label}${r.threshold ? `, threshold ${r.threshold}` : ''}`)) : null,
            a.state === 'resolved' ? null : h('div', { style: { display: 'flex', gap: '8px' } },
                a.state === 'firing' ? action('Acknowledge', (id) => api.ackAlert(id), 'Acknowledged', 'btn-primary') : null,
                action('Resolve', (id) => api.resolveAlert(id), 'Resolved', ''))));
}
