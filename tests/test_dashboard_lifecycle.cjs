const { test } = require('node:test');
const assert = require('node:assert/strict');
const { createRuntime } = require('../tools/dashboard-runtime.js');

function deferred() {
    let resolve, reject;
    const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
    return { promise, resolve, reject };
}

const response = body => ({ ok: true, status: 200, json: async () => body });
async function flush() { for (let i = 0; i < 20; i++) await Promise.resolve(); }

function environment(send) {
    const timers = new Map();
    const documentListeners = new Map();
    const signalListeners = new Set();
    let nextTimer = 1;
    class TrackedAbortController extends AbortController {
        constructor() {
            super();
            const signal = this.signal;
            const add = signal.addEventListener.bind(signal);
            const remove = signal.removeEventListener.bind(signal);
            const callbacks = new Map();
            signal.addEventListener = (type, callback, options) => {
                if (type !== 'abort') return add(type, callback, options);
                if (callbacks.has(callback)) return;
                const token = {};
                const wrapped = event => {
                    if (options?.once) {
                        callbacks.delete(callback);
                        signalListeners.delete(token);
                    }
                    if (typeof callback === 'function') callback.call(signal, event);
                    else callback.handleEvent(event);
                };
                callbacks.set(callback, { wrapped, token });
                signalListeners.add(token);
                add(type, wrapped, options);
            };
            signal.removeEventListener = (type, callback, options) => {
                const entry = callbacks.get(callback);
                if (type !== 'abort' || !entry) return remove(type, callback, options);
                callbacks.delete(callback);
                signalListeners.delete(entry.token);
                remove(type, entry.wrapped, options);
            };
        }
    }
    const document = {
        hidden: false,
        addEventListener: (name, callback) => documentListeners.set(name, callback),
        removeEventListener: (name, callback) => {
            if (documentListeners.get(name) === callback) documentListeners.delete(name);
        },
    };
    const runtime = createRuntime({
        document, fetch: send, AbortController: TrackedAbortController,
        setTimeout(callback, delay) {
            const id = nextTimer++;
            timers.set(id, { callback, delay });
            return id;
        },
        clearTimeout: id => timers.delete(id),
    });
    return {
        runtime, timers, document, documentListeners, signalListeners,
        Controller: TrackedAbortController,
        fire(delay) {
            const entry = [...timers].find(([, timer]) => timer.delay === delay);
            assert.ok(entry, `expected a timer at ${delay} ms`);
            timers.delete(entry[0]);
            return entry[1].callback();
        },
        assertClean() {
            assert.equal(timers.size, 0, 'runtime timers should be released');
            assert.equal(signalListeners.size, 0, 'abort listeners should be released');
        },
    };
}

function observe(promise) {
    const result = { state: 'pending' };
    promise.then(value => Object.assign(result, { state: 'fulfilled', value }),
        error => Object.assign(result, { state: 'rejected', error }));
    return result;
}

function assertRejected(result, name) {
    assert.equal(result.state, 'rejected', 'operation should settle without waiting for the transport');
    assert.equal(result.error.name, name);
}

test('deadline releases a GET even when the transport ignores abort, allowing another read', async t => {
    const stuck = deferred();
    let calls = 0;
    const env = environment(() => ++calls === 1 ? stuck.promise : Promise.resolve(response({ fresh: true })));
    t.after(() => env.runtime.dispose());
    const first = env.runtime.request('/synthetic', {}, { timeout: 25 });
    const state = observe(first);
    env.fire(25);
    await flush();
    assertRejected(state, 'TimeoutError');
    env.assertClean();
    const next = env.runtime.request('/synthetic', {}, { timeout: 25 });
    assert.notEqual(next, first);
    assert.deepEqual(await (await next).json(), { fresh: true });
    assert.equal(calls, 2);
    env.assertClean();
});

test('deadline includes JSON decoding even when the body ignores abort', async t => {
    const body = deferred();
    const env = environment(async () => ({ ok: true, status: 200, json: () => body.promise }));
    t.after(() => env.runtime.dispose());
    const state = observe(env.runtime.request('/synthetic', {}, { timeout: 25 }));
    await flush();
    env.fire(25);
    await flush();
    assertRejected(state, 'TimeoutError');
    env.assertClean();
});

for (const phase of ['transport', 'body']) {
    for (const cancel of ['page', 'dispose', 'external']) {
        test(`${cancel} cancellation settles an uncooperative ${phase} and releases runtime resources`, async t => {
            const stuck = deferred();
            const env = environment(phase === 'transport'
                ? () => stuck.promise
                : async () => ({ ok: true, status: 200, json: () => stuck.promise }));
            t.after(() => env.runtime.dispose());
            const caller = new env.Controller();
            const state = observe(env.runtime.request('/synthetic',
                cancel === 'external' ? { signal: caller.signal } : {},
                cancel === 'dispose' ? { global: true } : {}));
            await flush();
            if (cancel === 'page') env.runtime.beginPage();
            else if (cancel === 'dispose') env.runtime.dispose();
            else caller.abort();
            await flush();
            assertRejected(state, 'AbortError');
            env.assertClean();
            if (cancel === 'dispose') assert.equal(env.documentListeners.size, 0);
        });
    }
}

test('a pre-aborted caller neither sends a request nor receives an existing shared read', async t => {
    const active = deferred();
    let calls = 0;
    const env = environment(() => { calls++; return active.promise; });
    t.after(() => env.runtime.dispose());
    const pending = env.runtime.request('/synthetic');
    const signal = new env.Controller();
    signal.abort();
    const sameURL = observe(env.runtime.request('/synthetic', { signal: signal.signal }));
    const newURL = observe(env.runtime.request('/other-synthetic', { signal: signal.signal }));
    await flush();
    assertRejected(sameURL, 'AbortError');
    assertRejected(newURL, 'AbortError');
    assert.equal(calls, 1);
    active.resolve(response({ active: true }));
    assert.deepEqual(await (await pending).json(), { active: true });
    env.assertClean();
});

test('independent AbortSignals do not cancel another caller with the same URL', async t => {
    const sends = [];
    const env = environment((_url, init) => {
        const work = deferred();
        sends.push({ init, work });
        return work.promise;
    });
    t.after(() => env.runtime.dispose());
    const caller = new env.Controller();
    const first = env.runtime.request('/synthetic', { signal: caller.signal });
    const second = env.runtime.request('/synthetic');
    const firstState = observe(first);
    const secondState = observe(second);
    assert.notEqual(first, second);
    assert.equal(sends.length, 2);
    caller.abort();
    await flush();
    assertRejected(firstState, 'AbortError');
    assert.equal(secondState.state, 'pending');
    assert.equal(sends[1].init.signal.aborted, false);
    sends[1].work.resolve(response({ independent: true }));
    assert.deepEqual(await (await second).json(), { independent: true });
    env.assertClean();
});

test('different deadlines do not share one in-flight GET', async t => {
    const sends = [];
    const env = environment(() => { const work = deferred(); sends.push(work); return work.promise; });
    t.after(() => env.runtime.dispose());
    const first = env.runtime.request('/synthetic', {}, { timeout: 25 });
    const second = env.runtime.request('/synthetic', {}, { timeout: 50 });
    const firstState = observe(first);
    const secondState = observe(second);
    assert.notEqual(first, second);
    assert.equal(sends.length, 2);
    env.fire(25);
    await flush();
    assertRejected(firstState, 'TimeoutError');
    assert.equal(secondState.state, 'pending');
    sends[1].resolve(response({ later: true }));
    assert.deepEqual(await (await second).json(), { later: true });
    env.assertClean();
});

for (const init of [{ headers: { 'X-Synthetic-Variant': 'one' } }, { cache: 'no-store' }]) {
    test(`${Object.keys(init)[0]} options do not inherit an unrelated shared GET`, async t => {
        const sends = [];
        const env = environment((_url, options) => {
            const work = deferred();
            sends.push({ options, work });
            return work.promise;
        });
        t.after(() => env.runtime.dispose());
        const normal = env.runtime.request('/synthetic');
        const customized = env.runtime.request('/synthetic', init);
        assert.notEqual(normal, customized);
        assert.equal(sends.length, 2);
        for (const [key, value] of Object.entries(init)) assert.deepEqual(sends[1].options[key], value);
        sends[0].work.resolve(response({ variant: 'normal' }));
        sends[1].work.resolve(response({ variant: 'custom' }));
        assert.deepEqual(await (await normal).json(), { variant: 'normal' });
        assert.deepEqual(await (await customized).json(), { variant: 'custom' });
        env.assertClean();
    });
}

for (const phase of ['transport', 'body']) {
    for (const finish of ['resolve', 'reject']) {
        test(`late ${phase} ${finish} stays observed after deadline and cannot replace a new result`, async t => {
            const late = deferred();
            const unhandled = [];
            const onUnhandled = error => unhandled.push(error);
            process.on('unhandledRejection', onUnhandled);
            t.after(() => process.removeListener('unhandledRejection', onUnhandled));
            let calls = 0;
            let lateDecodes = 0;
            const env = environment(() => {
                calls++;
                if (calls > 1) return Promise.resolve(response({ current: true }));
                return phase === 'transport' ? late.promise
                    : Promise.resolve({ ok: true, status: 200, json: () => late.promise });
            });
            t.after(() => env.runtime.dispose());
            const expired = observe(env.runtime.request('/synthetic', {}, { timeout: 25 }));
            await flush();
            env.fire(25);
            await flush();
            assertRejected(expired, 'TimeoutError');
            const fresh = await env.runtime.request('/synthetic', {}, { timeout: 25 });
            if (finish === 'reject') late.reject(new Error('synthetic late transport error'));
            else late.resolve(phase === 'transport'
                ? { ok: true, status: 200, json: async () => { lateDecodes++; return { stale: true }; } }
                : { stale: true });
            await new Promise(resolve => setImmediate(resolve));
            assertRejected(expired, 'TimeoutError');
            assert.deepEqual(await fresh.json(), { current: true });
            assert.equal(lateDecodes, 0, 'a cancelled late response should not start body decoding');
            assert.deepEqual(unhandled, []);
            env.assertClean();
        });
    }
}

for (const finish of ['resolve', 'reject']) {
    test(`late ${finish} from an expired read cannot remove a newer pending read with the same key`, async t => {
        const old = deferred();
        const current = deferred();
        let calls = 0;
        const env = environment(() => ++calls === 1 ? old.promise : current.promise);
        t.after(() => env.runtime.dispose());
        const expired = observe(env.runtime.request('/synthetic', {}, { timeout: 25 }));
        env.fire(25);
        await flush();
        assertRejected(expired, 'TimeoutError');
        const newRead = env.runtime.request('/synthetic', {}, { timeout: 25 });
        if (finish === 'resolve') old.resolve(response({ stale: true }));
        else old.reject(new Error('synthetic late failure'));
        await flush();
        assert.equal(env.runtime.request('/synthetic', {}, { timeout: 25 }), newRead);
        assert.equal(calls, 2, 'the still-pending new read should remain shared');
        current.resolve(response({ current: true }));
        assert.deepEqual(await (await newRead).json(), { current: true });
        env.assertClean();
    });
}

test('a polling callback can recover from an uncooperative request deadline and visibility changes', async t => {
    const env = environment(() => new Promise(() => {}));
    t.after(() => env.runtime.dispose());
    let callbacks = 0;
    env.runtime.startPoll(async () => {
        callbacks++;
        await env.runtime.request('/synthetic', {}, { timeout: 25 });
    }, 5000);
    env.fire(5000);
    await flush();
    env.fire(25);
    await flush();
    assert.equal(callbacks, 1);
    assert.deepEqual([...env.timers.values()].map(timer => timer.delay), [5000]);
    env.document.hidden = true;
    env.documentListeners.get('visibilitychange')();
    env.assertClean();
    env.document.hidden = false;
    env.documentListeners.get('visibilitychange')();
    env.documentListeners.get('visibilitychange')();
    assert.deepEqual([...env.timers.values()].map(timer => timer.delay), [0]);
    env.fire(0);
    await flush();
    assert.equal(callbacks, 2);
    env.runtime.dispose();
    await flush();
    env.assertClean();
});

test('one thousand page visits release timers and abort listeners without breaking ordinary GET sharing', async t => {
    let calls = 0;
    const env = environment(() => { calls++; return new Promise(() => {}); });
    t.after(() => env.runtime.dispose());
    for (let visit = 0; visit < 1000; visit++) {
        const first = env.runtime.request('/synthetic');
        assert.equal(first, env.runtime.request('/synthetic', { method: 'GET' }));
        const state = observe(first);
        env.runtime.startPoll(async () => {}, 5000);
        env.runtime.beginPage();
        await flush();
        assertRejected(state, 'AbortError');
        env.assertClean();
        assert.equal(env.documentListeners.size, 1);
    }
    assert.equal(calls, 1000);
    env.runtime.dispose();
    await flush();
    env.assertClean();
    assert.equal(env.documentListeners.size, 0);
});
