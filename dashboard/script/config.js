export const config = {
    /** Where the dashboard API lives. Relative or absolute, no trailing slash. */
    baseUrl: '/api/v1/dashboard',

    /** Extra headers for every request, evaluated per call. Needed when PROCESSOR_API_KEY is set. */
    headers: () => ({
        'X-API-Key': localStorage.getItem('dashboard.apiKey') ?? '',
    }),

    /** Seconds between automatic refreshes while Live is on. */
    refreshSeconds: 30,

    /** Give up on a request after this long. */
    timeoutMs: 15000,
};
