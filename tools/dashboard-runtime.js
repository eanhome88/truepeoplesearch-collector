/* Shared request and polling lifecycle for the local dashboard. No dependencies. */
(function (root, factory) {
    if (typeof module === 'object' && module.exports) module.exports = factory();
    else root.LocalDashboard = factory();
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
    'use strict';

    function noop() {}

    function cancelled() {
        const error = new Error('页面已切换');
        error.name = 'AbortError';
        return error;
    }

    function createRuntime(options = {}) {
        const doc = options.document || document;
        const send = options.fetch || fetch.bind(globalThis);
        const later = options.setTimeout || setTimeout;
        const clear = options.clearTimeout || clearTimeout;
        const Controller = options.AbortController || AbortController;
        const pending = new Map();
        const polls = new Set();
        const requests = new Set();
        let page = { id: 0, controller: new Controller() };
        let disposed = false;

        function isCurrent(id) { return !disposed && page.id === id; }

        function beginPage() {
            if (disposed) return page.id;
            page.controller.abort();
            for (const poll of [...polls]) if (!poll.global) poll.stop();
            page = { id: page.id + 1, controller: new Controller() };
            return page.id;
        }

        function request(url, init = {}, config = {}) {
            if (disposed || init.signal?.aborted) return Promise.reject(cancelled());
            const scope = config.global ? null : page;
            const method = (init.method || 'GET').toUpperCase();
            const duration = config.timeout || (method === 'GET' ? 12000 : 30000);
            // Only share ordinary reads with the same deadline. Independent
            // cancellation, headers, credentials and cache options keep ownership.
            const shareable = method === 'GET' && Object.keys(init).every(key => key === 'method');
            const key = shareable ? JSON.stringify([scope ? scope.id : 'global', url, duration]) : null;
            if (key && pending.has(key)) return pending.get(key);
            const controller = new Controller();
            requests.add(controller);
            let timedOut = false;
            const abort = () => controller.abort();
            const signals = [scope?.controller.signal, init.signal].filter(Boolean);
            const endError = () => {
                if (disposed || (scope && !isCurrent(scope.id)) || signals.some(s => s.aborted)) return cancelled();
                if (timedOut) {
                    const error = new Error(method === 'GET'
                        ? '请求超时，请稍后重试'
                        : '请求超时，操作结果尚未确认；请刷新状态后再决定是否重试');
                    error.name = 'TimeoutError';
                    return error;
                }
                return cancelled();
            };
            const guard = () => {
                if (disposed || (scope && !isCurrent(scope.id)) || controller.signal.aborted || signals.some(s => s.aborted)) {
                    throw endError();
                }
            };
            let rejectOnAbort;
            const interrupted = new Promise((_, reject) => {
                rejectOnAbort = () => reject(endError());
                controller.signal.addEventListener('abort', rejectOnAbort, { once: true });
            });
            for (const signal of signals) {
                if (signal.aborted) controller.abort();
                else signal.addEventListener('abort', abort, { once: true });
            }
            const timeout = later(() => { timedOut = true; controller.abort(); }, duration);
            const transport = (async () => {
                guard();
                // Include body decoding in the timeout and stale-page check.
                const response = await send(url, { ...init, signal: controller.signal });
                guard();
                const body = await response.json();
                guard();
                return {
                    ok: response.ok,
                    status: response.status,
                    async json() { guard(); return body; },
                };
            })();
            // The runtime can settle and release its own slots even if a custom
            // transport ignores abort. Its late result/rejection remains observed.
            const operation = Promise.race([transport, interrupted]).catch(error => {
                guard();
                throw error;
            }).finally(() => {
                clear(timeout);
                requests.delete(controller);
                controller.signal.removeEventListener('abort', rejectOnAbort);
                for (const signal of signals) signal.removeEventListener('abort', abort);
                if (key && pending.get(key) === operation) pending.delete(key);
            });
            if (key) pending.set(key, operation);
            return operation;
        }

        function startPoll(callback, interval, config = {}) {
            // Late async completions may try to restart work after teardown.
            // Keep the public handle shape without retaining a callback or runtime.
            if (disposed) return { global: Boolean(config.global), stop: noop, visibility: noop };
            let timer = null;
            let running = false;
            let stopped = false;
            let failures = 0;
            const maxDelay = Math.max(interval, 30000);
            const pageId = page.id;
            const poll = {
                global: Boolean(config.global),
                stop() {
                    stopped = true;
                    if (timer !== null) clear(timer);
                    timer = null;
                    polls.delete(poll);
                },
                visibility() {
                    if (timer !== null) clear(timer);
                    timer = null;
                    if (!doc.hidden && !running) schedule(0);
                },
            };
            function active() {
                return !disposed && !stopped && (poll.global || isCurrent(pageId));
            }
            function schedule(delay) {
                if (!active() || doc.hidden || running) return;
                timer = later(tick, delay);
            }
            async function tick() {
                timer = null;
                if (!active() || doc.hidden || running) return;
                running = true;
                try {
                    const result = await callback();
                    failures = result === false ? Math.min(failures + 1, 20) : 0;
                } catch (error) {
                    // A page cancellation is not a service failure. Each UI
                    // section still owns its error display; mutations are never retried here.
                    if (!isCancelled(error)) failures = Math.min(failures + 1, 20);
                } finally {
                    running = false;
                    schedule(config.backoff ? Math.min(interval * (2 ** failures), maxDelay) : interval);
                }
            }
            polls.add(poll);
            schedule(interval);
            return poll;
        }

        const visibility = () => {
            for (const poll of polls) poll.visibility();
            options.onVisibilityChange?.(Boolean(doc.hidden));
        };
        doc.addEventListener('visibilitychange', visibility);
        options.onVisibilityChange?.(Boolean(doc.hidden));
        function dispose() {
            disposed = true;
            page.controller.abort();
            for (const controller of requests) controller.abort();
            for (const poll of [...polls]) poll.stop();
            doc.removeEventListener('visibilitychange', visibility);
        }
        return { request, startPoll, beginPage, isCurrent, dispose, currentPage: () => page.id };
    }

    function isCancelled(error) { return error?.name === 'AbortError'; }
    function setTextIfChanged(element, value) {
        if (!element) return false;
        const text = value == null ? '' : String(value);
        if (element.textContent === text) return false;
        element.textContent = text;
        return true;
    }
    return { createRuntime, isCancelled, setTextIfChanged };
});
