/* DOM helpers, formatters and icons. No dependencies. */

const SVG_NS = 'http://www.w3.org/2000/svg';

function applyProps(el, props) {
    for (const [k, v] of Object.entries(props || {})) {
        if (v === undefined || v === null || v === false) continue;
        if (k === 'class') el.setAttribute('class', v);
        else if (k === 'text') el.textContent = v;
        else if (k === 'style' && typeof v === 'object') {
            for (const [sk, sv] of Object.entries(v)) {
                if (sk.startsWith('--')) el.style.setProperty(sk, sv);
                else el.style[sk] = sv;
            }
        } else if (k === 'dataset') Object.assign(el.dataset, v);
        else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2).toLowerCase(), v);
        else el.setAttribute(k, v === true ? '' : v);
    }
}

function append(el, children) {
    for (const c of children.flat(Infinity)) {
        if (c === null || c === undefined || c === false) continue;
        el.append(c instanceof Node ? c : document.createTextNode(String(c)));
    }
}

/** Create an HTML element. Strings are always inserted as text, never as markup. */
export function h(tag, props, ...children) {
    const el = document.createElement(tag);
    applyProps(el, props);
    append(el, children);
    return el;
}

/** Create an SVG element. */
export function s(tag, props, ...children) {
    const el = document.createElementNS(SVG_NS, tag);
    applyProps(el, props);
    append(el, children);
    return el;
}

/* ---------------------------------------------------------------- icons */

const ICONS = {
    check: '<path d="M3 8.5l3.5 3.5L13 4.5"/>',
    triangle: '<path d="M8 2l6.5 11.5h-13z"/><path d="M8 6.5v3"/><path d="M8 11.7v.1"/>',
    xcircle: '<circle cx="8" cy="8" r="6"/><path d="M5.8 5.8l4.4 4.4M10.2 5.8l-4.4 4.4"/>',
    info: '<circle cx="8" cy="8" r="6"/><path d="M8 7.3v3.5"/><path d="M8 5v.1"/>',
    refresh: '<path d="M13 8a5 5 0 1 1-1.6-3.7"/><path d="M13 2.5v3h-3"/>',
    chevron: '<path d="M4 6l4 4 4-4"/>',
    up: '<path d="M8 13V3M4 7l4-4 4 4"/>',
    down: '<path d="M8 3v10M4 9l4 4 4-4"/>',
    flat: '<path d="M3 8h10"/>',
    bell: '<path d="M4 11V7a4 4 0 0 1 8 0v4l1 1.5H3z"/><path d="M6.5 14.2h3"/>',
    table: '<rect x="2" y="3" width="12" height="10" rx="1.5"/><path d="M2 7h12M6.5 7v6"/>',
    chart: '<path d="M2.5 13V3M2.5 13h11"/><path d="M5 10l3-3 2 2 3.5-4"/>',
    arrow: '<path d="M3 8h10M9 4l4 4-4 4"/>',
};

export function icon(name, size = 16) {
    const el = s('svg', {
        width: size, height: size, viewBox: '0 0 16 16', fill: 'none', stroke: 'currentColor',
        'stroke-width': 1.7, 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'aria-hidden': 'true', focusable: 'false',
    });
    el.innerHTML = ICONS[name] || ''; // static strings from the table above, never user data
    return el;
}

/* ------------------------------------------------------------- time ranges */

export const RANGES = [
    { id: '1h', label: '1h', long: 'hour', ms: 3600e3 },
    { id: '6h', label: '6h', long: '6 hours', ms: 6 * 3600e3 },
    { id: '24h', label: '24h', long: '24 hours', ms: 24 * 3600e3 },
    { id: '7d', label: '7d', long: '7 days', ms: 7 * 86400e3 },
    { id: '30d', label: '30d', long: '30 days', ms: 30 * 86400e3 },
];

export const rangeById = (id) => RANGES.find((r) => r.id === id) || RANGES[2];

/* -------------------------------------------------------------- formatters */

const trimZero = (n, d) => n.toFixed(d).replace(/\.0+$/, '').replace(/(\.\d*[1-9])0+$/, '$1');

/** 1,284 / 12.9K / 4.2M. Full digits below 10,000. */
export function fmtCompact(n) {
    if (n === null || n === undefined || Number.isNaN(n)) return '-';
    const a = Math.abs(n);
    if (a >= 1e9) return `${trimZero(n / 1e9, 1)}B`;
    if (a >= 1e6) return `${trimZero(n / 1e6, 1)}M`;
    if (a >= 1e4) return `${trimZero(n / 1e3, 1)}K`;
    if (a >= 1e3) return Math.round(n).toLocaleString('en-US');
    if (a >= 100 || Number.isInteger(n)) return String(Math.round(n));
    return trimZero(n, a >= 10 ? 1 : 2);
}

export const fmtInt = (n) => Math.round(n).toLocaleString('en-US');

export function fmtPct(n, digits = 2) {
    if (n === null || n === undefined || Number.isNaN(n)) return '-';
    return `${trimZero(n, digits)}%`;
}

export function fmtMs(n) {
    if (n === null || n === undefined || Number.isNaN(n)) return '-';
    return n >= 1000 ? `${trimZero(n / 1000, 2)} s` : `${Math.round(n)} ms`;
}

export function fmtDuration(ms) {
    const m = Math.round(ms / 60e3);
    if (m < 1) return 'under a minute';
    if (m < 60) return `${m}m`;
    const hrs = Math.floor(m / 60);
    if (hrs < 24) return m % 60 ? `${hrs}h ${m % 60}m` : `${hrs}h`;
    const d = Math.floor(hrs / 24);
    return hrs % 24 ? `${d}d ${hrs % 24}h` : `${d}d`;
}

export function fmtAgo(ts, now = Date.now()) {
    const diff = now - ts;
    if (diff < 45e3) return 'just now';
    return `${fmtDuration(diff)} ago`;
}

const clock = new Intl.DateTimeFormat('en-GB', { hour: '2-digit', minute: '2-digit' });
const day = new Intl.DateTimeFormat('en-GB', { day: 'numeric', month: 'short' });
const dayClock = new Intl.DateTimeFormat('en-GB', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });

export const fmtClock = (ts) => clock.format(ts);
export const fmtDay = (ts) => day.format(ts);
export const fmtDateTime = (ts) => dayClock.format(ts);

/** Axis tick format for a time range. */
export function axisTime(rangeId) {
    return rangeId === '7d' || rangeId === '30d' ? fmtDay : fmtClock;
}

/** Tooltip time format: always precise. */
export const tipTime = fmtDateTime;

export function debounce(fn, wait = 150) {
    let t;
    return (...args) => {
        clearTimeout(t);
        t = setTimeout(() => fn(...args), wait);
    };
}

/* ---------------------------------------------------------- status and severity */

// A source is judged by its firing alerts: critical = Critical, high or medium = Warning, otherwise Healthy.
// The keys stay ok / degraded / down because the pixel and chip styles are keyed on them.
export const STATUS = {
    ok: { label: 'Healthy', icon: 'check' },
    degraded: { label: 'Warning', icon: 'triangle' },
    down: { label: 'Critical', icon: 'xcircle' },
};

export const SEVERITY = {
    critical: { label: 'Critical', icon: 'xcircle' },
    high: { label: 'High', icon: 'triangle' },
    medium: { label: 'Medium', icon: 'triangle' },
    low: { label: 'Low', icon: 'info' },
};

export const SEVERITY_ORDER = ['critical', 'high', 'medium', 'low'];
