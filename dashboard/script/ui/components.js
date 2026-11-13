/* Shared building blocks used by every view. */

import { h, icon, STATUS, SEVERITY, rangeById, fmtCompact, fmtPct, fmtDateTime } from './dom.js';
import { lineChart, stackedBars, seriesColor, sparkline } from './charts.js';

/* ---------------------------------------------------------------------- chips */

export function statusChip(status) {
    const m = STATUS[status] || STATUS.ok;
    return h('span', { class: `chip chip-${status}` }, icon(m.icon, 14), m.label);
}

export function severityChip(sev) {
    const m = SEVERITY[sev] || SEVERITY.low;
    return h('span', { class: `chip chip-${sev}` }, icon(m.icon, 14), m.label);
}

export function stateChip(state) {
    const label = { firing: 'Firing', acknowledged: 'Acknowledged', resolved: 'Resolved' }[state] || state;
    return h('span', { class: `chip chip-plain is-${state}` }, label);
}

/* --------------------------------------------------------------------- panels */

export function panel({ title, subtitle, actions, class: cls = '' } = {}, ...body) {
    return h('section', { class: `panel ${cls}`.trim() },
        title || actions
            ? h('div', { class: 'panel-head' },
                h('div', null,
                    title ? h('h3', { class: 'panel-title' }, title) : null,
                    subtitle ? h('p', { class: 'panel-sub' }, subtitle) : null),
                actions ? h('div', { class: 'panel-actions' }, actions) : null)
            : null,
        ...body);
}

/** Chart and table views of the same series. Every chart can be read as a table. */
function dataTable(times, series, { format, timeFmt }) {
    return h('div', { class: 'table-wrap scroll' },
        h('table', { class: 'data' },
            h('thead', null, h('tr', null,
                h('th', null, 'Time'),
                series.map((se) => h('th', { class: 'num' }, se.name)))),
            h('tbody', null, times.map((t, i) => i).reverse().map((i) => h('tr', null,
                h('td', null, timeFmt(times[i])),
                series.map((se) => h('td', { class: 'num' }, format(se.values[i]))))))));
}

/**
 * kind: 'line' | 'bars'
 * data: { times, series: [{ id, name, values }] }
 * options: { yFormat, tipFormat, axisTime, threshold, area }
 */
export function chartCard({ title, subtitle, class: cls = '', kind = 'line', data, options = {} }) {
    const host = h('div', { class: 'chart-host' });
    const format = options.tipFormat || options.yFormat || fmtCompact;
    let asTable = false;

    const draw = () => {
        if (asTable) {
            host.replaceChildren(dataTable(data.times, data.series, { format, timeFmt: fmtDateTime }));
            return;
        }
        const run = kind === 'bars' ? stackedBars : lineChart;
        run(host, { ...options, times: data.times, series: data.series, label: title });
    };

    const toggle = h('button', {
        class: 'btn btn-sm',
        type: 'button',
        'aria-pressed': 'false',
        onclick: () => {
            asTable = !asTable;
            toggle.setAttribute('aria-pressed', String(asTable));
            toggle.replaceChildren(icon(asTable ? 'chart' : 'table', 14), asTable ? 'Chart' : 'Table');
            draw();
        },
    }, icon('table', 14), 'Table');

    const legend = data.series.length > 1
        ? h('div', { class: 'legend' }, data.series.map((se, i) => h('span', { class: 'legend-item' },
            h('span', { class: kind === 'bars' ? 'key key-box' : 'key', style: { '--key-color': se.color || seriesColor(i) } }),
            se.name)))
        : null;

    const card = panel({ title, subtitle, actions: toggle, class: cls }, legend, host);
    draw();
    return card;
}

/* ------------------------------------------------------------------ stat tile */

function splitValue(k) {
    if (k.unit === 'percent') return { num: fmtPct(k.value, 2).replace('%', ''), suffix: '%' };
    if (k.unit === 'ms') return k.value >= 1000 ? { num: (k.value / 1000).toFixed(2), suffix: 's' } : { num: String(Math.round(k.value)), suffix: 'ms' };
    return { num: fmtCompact(k.value), suffix: '' };
}

function delta(k, rangeId) {
    const prevLabel = `previous ${rangeById(rangeId).long}`;
    if (k.previous === null || k.previous === undefined) return null;

    const usePoints = k.id === 'availability';
    const change = usePoints ? k.value - k.previous : k.previous ? ((k.value - k.previous) / k.previous) * 100 : 0;
    const flat = usePoints ? Math.abs(change) < 0.005 : Math.abs(change) < 0.5;
    const dir = flat ? 'flat' : change > 0 ? 'up' : 'down';
    const good = flat || k.higherIsBetter === null || k.higherIsBetter === undefined ? null : (change > 0) === k.higherIsBetter;
    const text = flat ? 'No change' : usePoints ? `${Math.abs(change).toFixed(2)} pts` : `${Math.abs(change) >= 100 ? Math.round(Math.abs(change)) : Math.abs(change).toFixed(1)}%`;

    return h('div', { class: 'tile-delta' },
        h('span', { class: `delta-value ${good === true ? 'delta-good' : good === false ? 'delta-bad' : ''}` },
            icon(dir === 'up' ? 'up' : dir === 'down' ? 'down' : 'flat', 13), text,
            h('span', { class: 'sr-only' }, dir === 'up' ? ' higher' : dir === 'down' ? ' lower' : '')),
        `vs ${prevLabel}`);
}

export function statTile(k, rangeId) {
    const { num, suffix } = splitValue(k);
    return h('div', { class: 'tile' },
        h('div', { class: 'tile-label' }, k.label),
        h('div', { class: 'tile-value' }, num, suffix ? h('small', null, suffix) : null),
        k.spark?.length > 1 ? h('div', { class: 'tile-spark' }, sparkline(k.spark, { label: `${k.label} trend` })) : null,
        delta(k, rangeId));
}

/* ------------------------------------------------------------------- controls */

export function segmented({ label, options, value, onChange }) {
    const group = h('div', { class: 'segmented', role: 'group', 'aria-label': label });
    const paint = (v) => {
        for (const b of group.children) b.setAttribute('aria-pressed', String(b.dataset.id === v));
    };
    for (const o of options) {
        group.append(h('button', {
            type: 'button',
            dataset: { id: o.id },
            'aria-pressed': String(o.id === value),
            onclick: () => {
                paint(o.id);
                onChange(o.id);
            },
        }, o.label));
    }
    return group;
}

export function selectControl({ label, options, value, onChange }) {
    const select = h('select', { class: 'select', 'aria-label': label, onchange: (e) => onChange(e.target.value) },
        options.map((o) => h('option', { value: o.id, selected: o.id === value }, o.label)));
    return h('label', { class: 'field' }, label, select);
}

export function searchInput({ placeholder, value = '', onInput }) {
    return h('input', {
        class: 'input', type: 'search', placeholder, value, 'aria-label': placeholder, autocomplete: 'off',
        oninput: (e) => onInput(e.target.value),
    });
}

/* --------------------------------------------------------- empty, error, toast */

export function emptyState({ title, message, action, error = false }) {
    return h('div', { class: `empty${error ? ' is-error' : ''}` },
        h('h3', null, title),
        message ? h('p', null, message) : null,
        action ? h('button', { class: 'btn', type: 'button', onclick: action.onClick }, action.label) : null);
}

export function errorState(err, retry) {
    return panel({}, emptyState({
        error: true,
        title: "Can't load this view",
        message: err?.message || 'Something went wrong while loading data.',
        action: { label: 'Try again', onClick: retry },
    }));
}

export function toast(message, { error = false } = {}) {
    const host = document.getElementById('toasts');
    if (!host) return;
    const el = h('div', { class: `toast${error ? ' is-error' : ''}` }, icon(error ? 'xcircle' : 'check', 16), message);
    host.append(el);
    setTimeout(() => el.remove(), 4200);
}
