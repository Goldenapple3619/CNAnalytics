import { config } from '../config.js';

export class ApiError extends Error {
    constructor(message, { status = 0, path = '' } = {}) {
        super(message);
        this.name = 'ApiError';
        this.status = status;
        this.path = path;
    }
}

/** Drop empty values and "all", so a missing parameter always means "no filter". */
function clean(query) {
    const out = {};
    for (const [k, v] of Object.entries(query || {})) {
        if (v === undefined || v === null || v === '' || v === 'all') continue;
        out[k] = v;
    }
    return out;
}

function toQuery(query) {
    const s = new URLSearchParams(clean(query)).toString();
    return s ? `?${s}` : '';
}

function withTimeout(signal) {
    if (typeof AbortSignal === 'undefined' || !AbortSignal.timeout) return signal;
    const timeout = AbortSignal.timeout(config.timeoutMs);
    return signal && AbortSignal.any ? AbortSignal.any([signal, timeout]) : (signal || timeout);
}

async function request(method, path, { query, body, signal } = {}) {
    let res;
    try {
        res = await fetch(`${config.baseUrl}${path}${toQuery(query)}`, {
            method,
            signal: withTimeout(signal),
            headers: {
                Accept: 'application/json',
                ...(body ? { 'Content-Type': 'application/json' } : {}),
                ...config.headers(),
            },
            body: body ? JSON.stringify(body) : undefined,
        });
    } catch (err) {
        if (signal?.aborted) throw err;
        if (err.name === 'TimeoutError') {
            throw new ApiError(`${method} ${path} took longer than ${config.timeoutMs / 1000} seconds.`, { path });
        }
        throw new ApiError(`Can't reach ${config.baseUrl}. Check that your backend is running and allows this origin.`, { path });
    }

    if (!res.ok) {
        let detail = '';
        try {
            const json = await res.json();
            // { message } is the documented shape; the processor (FastAPI) answers { detail }.
            const d = json.message || json.error || json.detail;
            detail = typeof d === 'string' ? d : d?.message || '';
        } catch { /* body was not JSON */ }
        throw new ApiError(detail || `${method} ${path} returned ${res.status}.`, { status: res.status, path });
    }

    return res.status === 204 ? null : res.json();
}

export const api = {
    /** GET /apps */
    apps: (opts) => request('GET', '/apps', opts),

    /** GET /overview?app&source&range */
    overview: (query, opts) => request('GET', '/overview', { query, ...opts }),

    /** GET /sources?app&source&range */
    sources: (query, opts) => request('GET', '/sources', { query, ...opts }),

    /** GET /events?app&source&range&type */
    events: (query, opts) => request('GET', '/events', { query, ...opts }),

    /** GET /alerts?app&source&state&severity&limit */
    alerts: (query, opts) => request('GET', '/alerts', { query, ...opts }),

    /** POST /alerts/:id/ack */
    ackAlert: (id, opts) => request('POST', `/alerts/${encodeURIComponent(id)}/ack`, opts),

    /** POST /alerts/:id/resolve */
    resolveAlert: (id, opts) => request('POST', `/alerts/${encodeURIComponent(id)}/resolve`, opts),
};
