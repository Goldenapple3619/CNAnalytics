/**
 * The only file you need to edit to point the dashboard at your backend.
 *
 * The backend is the analytics processor (../processor). Its dashboard endpoints live under
 * /api/v1/dashboard. `node dev-server.mjs` forwards /api/* to the processor, so the default
 * works as is; with any other static server, put the processor's full URL here, for example
 * 'http://127.0.0.1:8080/api/v1/dashboard' (the processor allows any origin by default).
 */
export const config = {
    /** Where the dashboard API lives. Relative or absolute, no trailing slash. */
    baseUrl: '/api/v1/dashboard',

    /** Extra headers for every request, evaluated per call. Needed when PROCESSOR_API_KEY is set. */
    headers: () => ({
        // 'X-API-Key': localStorage.getItem('dashboard.apiKey') ?? '',
    }),

    /** Seconds between automatic refreshes while Live is on. */
    refreshSeconds: 30,

    /** Give up on a request after this long. */
    timeoutMs: 15000,
};
