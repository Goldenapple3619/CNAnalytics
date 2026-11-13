import { h } from './dom.js';

/**
 * One shared tooltip for every chart. Content is built with DOM nodes and
 * textContent, so labels that come from your API can never inject markup.
 */

export function tipContent({ title, rows = [], foot }) {
    return h('div', null,
        title ? h('div', { class: 'tip-title' }, title) : null,
        rows.map((r) => h('div', { class: 'tip-row' },
            r.color
                ? h('span', { class: 'key', style: { '--key-color': r.color } })
                : h('span'),
            h('span', { class: 'tip-value' }, r.value),
            h('span', { class: 'tip-label' }, r.label),
        )),
        foot ? h('div', { class: 'tip-foot' }, foot) : null,
    );
}

/** Show the tooltip next to a viewport point (x, y). */
export function showTip(x, y, node) {
    const el = document.getElementById('tooltip');
    if (!el) return;
    el.replaceChildren(node);
    el.hidden = false;

    const { width, height } = el.getBoundingClientRect();
    const pad = 8;
    let left = x + 16;
    if (left + width > window.innerWidth - pad) left = x - 16 - width;
    left = Math.max(pad, left);
    const top = Math.min(Math.max(pad, y - height / 2), window.innerHeight - height - pad);
    el.style.left = `${left}px`;
    el.style.top = `${top}px`;
}

export function hideTip() {
    const el = document.getElementById('tooltip');
    if (el) el.hidden = true;
}
