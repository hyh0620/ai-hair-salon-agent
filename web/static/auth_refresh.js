(function () {
    'use strict';

    let csrfCookieName = 'salon_csrf_token';
    let refreshPromise = null;
    const excludedAuthPaths = new Set([
        '/api/auth/login',
        '/api/auth/register',
        '/api/auth/refresh',
        '/api/auth/logout'
    ]);

    function configure(options = {}) {
        if (typeof options.csrfCookieName === 'string' && options.csrfCookieName) {
            csrfCookieName = options.csrfCookieName;
        }
    }

    function readCookie(name) {
        const prefix = `${encodeURIComponent(name)}=`;
        const entry = document.cookie
            .split('; ')
            .find((item) => item.startsWith(prefix));
        return entry ? decodeURIComponent(entry.slice(prefix.length)) : '';
    }

    function csrfToken() {
        return readCookie(csrfCookieName);
    }

    function shouldAttemptRefresh(request) {
        const url = new URL(request.url, window.location.origin);
        return url.origin === window.location.origin && !excludedAuthPaths.has(url.pathname);
    }

    function retryDelay(response) {
        const raw = response.headers.get('Retry-After') || '';
        if (!/^\d+$/.test(raw)) return 1000;
        return Math.min(Math.max(Number(raw), 1), 3) * 1000;
    }

    function wait(milliseconds) {
        return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
    }

    async function performRefresh() {
        for (let attempt = 0; attempt < 2; attempt += 1) {
            const token = csrfToken();
            if (!token) return null;
            const response = await window.fetch('/api/auth/refresh', {
                method: 'POST',
                credentials: 'same-origin',
                headers: {'X-CSRF-Token': token}
            });
            if (response.status !== 409 || attempt === 1) return response;
            await wait(retryDelay(response));
        }
        return null;
    }

    function refreshSingleFlight() {
        if (!refreshPromise) {
            refreshPromise = performRefresh().finally(() => {
                refreshPromise = null;
            });
        }
        return refreshPromise;
    }

    function notifySessionExpired() {
        window.dispatchEvent(new CustomEvent('salon-auth-expired', {
            detail: {message: '登录状态已失效，请重新登录'}
        }));
    }

    function withCurrentCsrf(request) {
        const headers = new Headers(request.headers);
        if (headers.has('X-CSRF-Token')) {
            const token = csrfToken();
            if (token) headers.set('X-CSRF-Token', token);
        }
        return new Request(request, {headers});
    }

    async function fetchWithAuthRefresh(input, init) {
        const request = new Request(input, init);
        const retryRequest = request.clone();
        const response = await window.fetch(request);
        if (response.status !== 401 || !shouldAttemptRefresh(request)) {
            return response;
        }

        const refreshResponse = await refreshSingleFlight();
        if (!refreshResponse || !refreshResponse.ok) {
            if (refreshResponse && refreshResponse.status === 401) {
                notifySessionExpired();
            }
            return response;
        }
        return window.fetch(withCurrentCsrf(retryRequest));
    }

    window.SalonAuthRefresh = Object.freeze({
        configure,
        csrfToken,
        fetch: fetchWithAuthRefresh
    });
}());
