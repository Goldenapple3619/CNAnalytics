/**
 * Small dependency-free SVG charts: line, stacked bars, horizontal bars,
 * sparkline and the pixel health strip.
 *
 * Every chart that draws a plot has a hover tooltip that also works from the
 * keyboard (focus the chart, then use the left and right arrow keys).
 */
import { h, s, fmtCompact, fmtDateTime, STATUS } from './dom.js';
import { showTip, hideTip, tipContent } from './tooltip.js';

export const seriesColor = (i) => `var(--s${(i % 8) + 1})`;

const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

/** Round axis ends to clean numbers: 0 / 50 / 100 / 150. */
export function niceScale(max, target = 4) {
    if (!(max > 0)) return { max: 1, ticks: [0, 0.5, 1] };
    const raw = max / target;
    const pow = 10 ** Math.floor(Math.log10(raw));
    const f = raw / pow;
    const step = (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * pow;
    const top = Math.ceil(max / step - 1e-9) * step;
    const ticks = [];
    for (let v = 0; v <= top + step / 2; v += step) ticks.push(+v.toFixed(10));
    return { max: ticks[ticks.length - 1], ticks };
}

/** Redraw when the host width changes. Returns nothing; the observer lives with the element. */
function onWidth(host, draw) {
    let last = 0;
    const ro = new ResizeObserver(() => {
        const w = Math.floor(host.clientWidth);
        if (w && w !== last) {
            last = w;
            draw(w);
        }
    });
    ro.observe(host);
}

function frame(host, o, drawPlot) {
    const H = o.height ?? 230;
    const svg = s('svg', { class: 'chart', height: H, role: 'group', 'aria-label': o.label || '', tabindex: 0 });
    host.replaceChildren(svg);

    let controller = null;
    let idx = null;
    svg.addEventListener('keydown', (e) => {
        if (!controller) return;
        if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
            e.preventDefault();
            idx = clamp((idx ?? controller.count) + (e.key === 'ArrowLeft' ? -1 : 1), 0, controller.count - 1);
            controller.show(idx, true);
        } else if (e.key === 'Escape') {
            controller.hide();
        }
    });
    svg.addEventListener('focus', () => {
        if (!controller) return;
        idx = controller.count - 1;
        if (!svg.matches(':hover')) controller.show(idx, true);
    });
    svg.addEventListener('blur', () => controller?.hide());

    onWidth(host, (W) => {
        svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
        svg.replaceChildren();
        controller = drawPlot(svg, W, H, (c) => { idx = c; });
    });
}

function axes(svg, { W, H, mt, mb, mr, ml, pw, ph, scale, yFmt, xTicks }) {
    for (const t of scale.ticks) {
        const y = mt + ph * (1 - t / scale.max);
        svg.append(
            s('line', { class: 'grid-line', x1: ml, x2: W - mr, y1: y, y2: y }),
            s('text', { class: 'tick', x: ml - 8, y: y + 4, 'text-anchor': 'end' }, yFmt(t)),
        );
    }
    for (const { x, label, anchor } of xTicks) {
        svg.append(s('text', { class: 'tick', x, y: H - mb + 17, 'text-anchor': anchor }, label));
    }
}

function xTickList(count, x, label, pw) {
    const k = clamp(Math.floor(pw / 96), 2, 8);
    const out = [];
    for (let j = 0; j <= k; j++) {
        const i = Math.round((j * (count - 1)) / k);
        out.push({ x: x(i), label: label(i), anchor: j === 0 ? 'start' : j === k ? 'end' : 'middle' });
    }
    return out;
}

/* ------------------------------------------------------------------ line chart */

/**
 * o: { times, series: [{ name, values, color? }], yFormat, tipFormat, axisTime,
 *      threshold: { value, label }, area, height, label }
 */
export function lineChart(host, o) {
    const yFmt = o.yFormat || fmtCompact;
    const tipFmt = o.tipFormat || yFmt;
    const n = o.times.length;
    const mt = 12, mb = 26, mr = 14;

    frame(host, o, (svg, W, H) => {
        const max = Math.max(0, ...o.series.flatMap((x) => x.values), o.threshold?.value ?? 0);
        const scale = niceScale(max * 1.05);
        const ml = Math.max(36, Math.max(...scale.ticks.map((t) => yFmt(t).length)) * 6.8 + 14);
        const pw = W - ml - mr;
        const ph = H - mt - mb;
        const x = (i) => ml + (n === 1 ? pw / 2 : (i * pw) / (n - 1));
        const y = (v) => mt + ph * (1 - v / scale.max);

        axes(svg, { W, H, mt, mb, mr, ml, pw, ph, scale, yFmt, xTicks: xTickList(n, x, (i) => o.axisTime(o.times[i]), pw) });

        if (o.threshold) {
            const ty = y(o.threshold.value);
            svg.append(
                s('line', { class: 'threshold', x1: ml, x2: W - mr, y1: ty, y2: ty }),
                s('text', { class: 'threshold-label', x: W - mr, y: ty - 6, 'text-anchor': 'end' }, `${o.threshold.label} ${yFmt(o.threshold.value)}`),
            );
        }

        const colors = o.series.map((se, i) => se.color || seriesColor(i));
        const path = (vals) => vals.map((v, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join('');

        if (o.area && o.series.length === 1) {
            const vals = o.series[0].values;
            svg.append(s('path', { d: `${path(vals)}L${x(n - 1)},${y(0)}L${x(0)},${y(0)}Z`, style: { fill: colors[0] }, 'fill-opacity': 0.12 }));
        }
        o.series.forEach((se, i) => {
            svg.append(s('path', {
                d: path(se.values), fill: 'none', 'stroke-width': 2, 'stroke-linejoin': 'round', 'stroke-linecap': 'round', style: { stroke: colors[i] },
            }));
        });
        o.series.forEach((se, i) => {
            svg.append(s('circle', {
                cx: x(n - 1), cy: y(se.values[n - 1]), r: 4, 'stroke-width': 2, style: { fill: colors[i], stroke: 'var(--surface)' },
            }));
        });

        const cross = s('line', { class: 'cross', y1: mt, y2: mt + ph, display: 'none' });
        const dots = o.series.map((_, i) => s('circle', { r: 4.5, 'stroke-width': 2, display: 'none', style: { fill: colors[i], stroke: 'var(--surface)' } }));
        const overlay = s('rect', { x: ml, y: mt, width: pw, height: ph, fill: 'transparent' });
        svg.append(cross, ...dots, overlay);

        const ctl = {
            count: n,
            show(i) {
                const cx = x(i);
                cross.setAttribute('x1', cx);
                cross.setAttribute('x2', cx);
                cross.removeAttribute('display');
                o.series.forEach((se, k) => {
                    dots[k].setAttribute('cx', cx);
                    dots[k].setAttribute('cy', y(se.values[i]));
                    dots[k].removeAttribute('display');
                });
                const rect = svg.getBoundingClientRect();
                const top = Math.min(...o.series.map((se) => y(se.values[i])));
                showTip(rect.left + cx, rect.top + top, tipContent({
                    title: fmtDateTime(o.times[i]),
                    rows: o.series.map((se, k) => ({ color: colors[k], value: tipFmt(se.values[i]), label: se.name })),
                }));
            },
            hide() {
                cross.setAttribute('display', 'none');
                dots.forEach((d) => d.setAttribute('display', 'none'));
                hideTip();
            },
        };
        const at = (e) => {
            const r = svg.getBoundingClientRect();
            return clamp(Math.round(((e.clientX - r.left - ml) / pw) * (n - 1)), 0, n - 1);
        };
        overlay.addEventListener('pointermove', (e) => ctl.show(at(e)));
        overlay.addEventListener('pointerleave', () => ctl.hide());
        return ctl;
    });
}

/* ---------------------------------------------------------------- stacked bars */

function topRounded(x, y, w, hgt, r) {
    const rr = Math.min(r, w / 2, hgt);
    return `M${x},${y + hgt}V${y + rr}Q${x},${y} ${x + rr},${y}H${x + w - rr}Q${x + w},${y} ${x + w},${y + rr}V${y + hgt}Z`;
}

/** o: { times, series: [{ name, values }], yFormat, axisTime, height, label } */
export function stackedBars(host, o) {
    const yFmt = o.yFormat || fmtCompact;
    const n = o.times.length;
    const mt = 12, mb = 26, mr = 14, GAP = 2;

    frame(host, o, (svg, W, H) => {
        const totals = o.times.map((_, i) => o.series.reduce((a, se) => a + se.values[i], 0));
        const scale = niceScale(Math.max(...totals) * 1.05);
        const ml = Math.max(36, Math.max(...scale.ticks.map((t) => yFmt(t).length)) * 6.8 + 14);
        const pw = W - ml - mr;
        const ph = H - mt - mb;
        const slot = pw / n;
        const bw = clamp(slot - GAP, 2, 24);
        const bx = (i) => ml + i * slot + (slot - bw) / 2;
        const cx = (i) => ml + i * slot + slot / 2;

        axes(svg, { W, H, mt, mb, mr, ml, pw, ph, scale, yFmt, xTicks: xTickList(n, cx, (i) => o.axisTime(o.times[i]), pw) });

        const band = s('rect', { class: 'band', y: mt, width: slot, height: ph, rx: 3, display: 'none' });
        svg.append(band);

        for (let i = 0; i < n; i++) {
            let acc = 0;
            const lastNonZero = o.series.reduce((a, se, k) => (se.values[i] > 0 ? k : a), -1);
            o.series.forEach((se, k) => {
                const v = se.values[i];
                if (v <= 0) return;
                const full = (v / scale.max) * ph;
                const bottom = mt + ph - (acc / scale.max) * ph;
                acc += v;
                const isTop = k === lastNonZero;
                const hgt = Math.max(1, full - (isTop ? 0 : GAP));
                const color = se.color || seriesColor(k);
                svg.append(isTop
                    ? s('path', { class: 'bar-seg', d: topRounded(bx(i), bottom - hgt, bw, hgt, 4), style: { fill: color } })
                    : s('rect', { class: 'bar-seg', x: bx(i), y: bottom - hgt, width: bw, height: hgt, style: { fill: color } }));
            });
        }

        const overlay = s('rect', { x: ml, y: mt, width: pw, height: ph, fill: 'transparent' });
        svg.append(overlay);

        const ctl = {
            count: n,
            show(i) {
                band.setAttribute('x', ml + i * slot);
                band.removeAttribute('display');
                const rect = svg.getBoundingClientRect();
                const top = mt + ph - (totals[i] / scale.max) * ph;
                showTip(rect.left + cx(i), rect.top + top, tipContent({
                    title: fmtDateTime(o.times[i]),
                    rows: [...o.series].map((se, k) => ({ color: se.color || seriesColor(k), value: yFmt(se.values[i]), label: se.name })).reverse(),
                    foot: `${yFmt(totals[i])} in total`,
                }));
            },
            hide() {
                band.setAttribute('display', 'none');
                hideTip();
            },
        };
        overlay.addEventListener('pointermove', (e) => {
            const r = svg.getBoundingClientRect();
            ctl.show(clamp(Math.floor((e.clientX - r.left - ml) / slot), 0, n - 1));
        });
        overlay.addEventListener('pointerleave', () => ctl.hide());
        return ctl;
    });
}

/* ------------------------------------------------------------ horizontal bars */

/** items: [{ name, value, sub }] */
export function hBars(items, { format = fmtCompact, noun = 'errors' } = {}) {
    const max = Math.max(...items.map((i) => i.value), 1);
    const total = items.reduce((a, i) => a + i.value, 0);

    return h('div', { class: 'hbars', role: 'list' }, items.map((it) => {
        const tip = (x, y) => showTip(x, y, tipContent({
            title: it.name,
            rows: [{ value: format(it.value), label: noun }],
            foot: `${Math.round((it.value / total) * 100)}% of the total${it.sub ? ` in ${it.sub}` : ''}`,
        }));
        return h('div', {
            class: 'hbar',
            role: 'listitem',
            tabindex: 0,
            onpointermove: (e) => tip(e.clientX, e.clientY),
            onpointerleave: hideTip,
            onfocus: (e) => {
                const r = e.currentTarget.getBoundingClientRect();
                tip(r.left + r.width / 2, r.top + r.height / 2);
            },
            onblur: hideTip,
        },
        h('span', { class: 'hbar-name' }, it.name),
        h('span', { class: 'hbar-track' }, h('span', { class: 'hbar-fill', style: { display: 'block', width: `${(it.value / max) * 100}%` } })),
        h('span', { class: 'hbar-value' }, format(it.value)));
    }));
}

/* ------------------------------------------------------------------ sparkline */

export function sparkline(values, { w = 92, h: hgt = 30, label } = {}) {
    const pad = 5;
    const min = Math.min(...values);
    const max = Math.max(...values);
    const span = max - min || 1;
    const x = (i) => pad + (i * (w - 2 * pad)) / (values.length - 1 || 1);
    const y = (v) => (max === min ? hgt / 2 : pad + (hgt - 2 * pad) * (1 - (v - min) / span));
    const d = values.map((v, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join('');

    return s('svg', { width: w, height: hgt, viewBox: `0 0 ${w} ${hgt}`, role: label ? 'img' : null, 'aria-label': label || null, 'aria-hidden': label ? null : 'true' },
        s('path', { d, fill: 'none', 'stroke-width': 2, 'stroke-linejoin': 'round', 'stroke-linecap': 'round', style: { stroke: 'var(--ink-3)' } }),
        s('circle', { cx: x(values.length - 1), cy: y(values[values.length - 1]), r: 3.5, style: { fill: 'var(--accent)' } }));
}

/* ----------------------------------------------------------------- health strip */

/** cells: [{ t, status }]. Block height carries the state as well as the colour. */
export function healthStrip(cells, { name }) {
    const count = (st) => cells.filter((c) => c.status === st).length;
    const down = count('down');
    const degraded = count('degraded');
    const summary = down || degraded
        ? `${name}: ${down} down and ${degraded} degraded periods`
        : `${name}: healthy in every period`;

    return h('div', { class: 'strip', style: { '--cells': cells.length }, role: 'img', 'aria-label': summary },
        cells.map((c) => h('span', {
            class: `cell cell-${c.status}`,
            onpointermove: (e) => showTip(e.clientX, e.clientY, tipContent({
                title: fmtDateTime(c.t),
                rows: [{ value: STATUS[c.status]?.label ?? 'No data', label: name }],
            })),
            onpointerleave: hideTip,
        })));
}
