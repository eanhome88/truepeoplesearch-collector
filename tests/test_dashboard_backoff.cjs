const { test } = require('node:test');
const assert = require('node:assert/strict');
const { createRuntime } = require('../tools/dashboard-runtime.js');

// These tests use synthetic callbacks and a virtual clock. They do not contact
// the API or include any manual first-screen refresh in their request counts.
async function flush() {
    for (let i = 0; i < 20; i++) await Promise.resolve();
}

function deferred() {
    let resolve;
    const promise = new Promise(yes => { resolve = yes; });
    return { promise, resolve };
}

function environment() {
    let now = 0;
    let serial = 0;
    const timers = new Map();
    const listeners = new Map();
    const document = {
        hidden: false,
        addEventListener(name, listener) { listeners.set(name, listener); },
        removeEventListener(name, listener) {
            if (listeners.get(name) === listener) listeners.delete(name);
        },
    };
    const runtime = createRuntime({
        document,
        fetch() { throw new Error('Polling tests must not use a real transport'); },
        setTimeout(fn, delay) {
            const id = ++serial;
            timers.set(id, { fn, at: now + delay });
            return id;
        },
        clearTimeout(id) { timers.delete(id); },
    });
    return {
        runtime, timers, listeners,
        now: () => now,
        delays: () => [...timers.values()].map(timer => timer.at - now).sort((a, b) => a - b),
        visibility(hidden) {
            document.hidden = hidden;
            listeners.get('visibilitychange')?.();
        },
        async advance(duration) {
            const target = now + duration;
            let fired = 0;
            for (;;) {
                const entry = [...timers].sort((a, b) => a[1].at - b[1].at || a[0] - b[0])[0];
                if (!entry || entry[1].at > target) break;
                assert.ok(++fired < 10000, 'a poll must not spin continuously at zero delay');
                timers.delete(entry[0]);
                now = entry[1].at;
                // Do not await: a pending callback must not prevent the clock
                // from advancing or hide overlap and late-cleanup regressions.
                entry[1].fn();
                await flush();
            }
            now = target;
            await flush();
        },
    };
}

test('fixed failure polling keeps the original cadence when backoff is not enabled', async t => {
    const env = environment();
    t.after(() => env.runtime.dispose());
    let calls = 0;
    env.runtime.startPoll(async () => { calls++; return false; }, 2000);
    await env.advance(60000);
    assert.equal(calls, 30, '60 seconds of 2-second polls, without manual initial refresh');
    assert.deepEqual(env.delays(), [2000]);
});

for (const failure of ['false', 'throw']) {
    test(`${failure} backs off 2-second polling to at most one attempt per 30 seconds`, async t => {
        const env = environment();
        t.after(() => env.runtime.dispose());
        const times = [];
        env.runtime.startPoll(async () => {
            times.push(env.now());
            if (failure === 'throw') throw new Error('synthetic status service offline');
            return false;
        }, 2000, { backoff: true });
        await env.advance(60000);
        assert.deepEqual(times, [2000, 6000, 14000, 30000, 60000]);
        assert.equal(times.length, 5, 'automatic polls only; no manual first-screen request');
        assert.deepEqual(env.delays(), [30000]);
        await env.advance(90000);
        assert.deepEqual(times.slice(5), [90000, 120000, 150000]);
        assert.deepEqual(env.delays(), [30000]);
    });
}

for (const success of [true, undefined]) {
    test(`successful ${String(success)} result restores the base cadence after failures`, async t => {
        const env = environment();
        t.after(() => env.runtime.dispose());
        let calls = 0;
        env.runtime.startPoll(async () => ++calls === 3 ? success : false, 2000, { backoff: true });
        await env.advance(14000);
        assert.equal(calls, 3);
        assert.deepEqual(env.delays(), [2000]);
        await env.advance(2000);
        assert.equal(calls, 4);
        assert.deepEqual(env.delays(), [4000], 'a new failure starts from the base interval again');
    });
}

test('a base interval above 30 seconds is never shortened by the cap', async t => {
    const env = environment();
    t.after(() => env.runtime.dispose());
    env.runtime.startPoll(async () => false, 45000, { backoff: true });
    await env.advance(45000);
    assert.deepEqual(env.delays(), [45000]);
    await env.advance(45000);
    assert.deepEqual(env.delays(), [45000]);
});

test('cancellation neither increases nor resets an existing failure delay', async t => {
    const env = environment();
    t.after(() => env.runtime.dispose());
    let calls = 0;
    env.runtime.startPoll(async () => {
        calls++;
        if (calls === 2 || calls === 3) throw Object.assign(new Error('synthetic cancellation'), { name: 'AbortError' });
        return calls === 1 ? false : true;
    }, 2000, { backoff: true });
    await env.advance(2000);
    assert.deepEqual(env.delays(), [4000]);
    await env.advance(8000);
    assert.equal(calls, 3);
    assert.deepEqual(env.delays(), [4000]);
    await env.advance(4000);
    assert.equal(calls, 4);
    assert.deepEqual(env.delays(), [2000]);
});

test('timeouts count as failures instead of being mistaken for cancellation', async t => {
    const env = environment();
    t.after(() => env.runtime.dispose());
    env.runtime.startPoll(async () => {
        throw Object.assign(new Error('synthetic timeout'), { name: 'TimeoutError' });
    }, 2000, { backoff: true });
    await env.advance(2000);
    assert.deepEqual(env.delays(), [4000]);
});

test('hidden windows pause retries and returning visible schedules one immediate check', async t => {
    const env = environment();
    t.after(() => env.runtime.dispose());
    const times = [];
    env.runtime.startPoll(async () => { times.push(env.now()); return false; }, 2000, { backoff: true });
    await env.advance(2000);
    env.visibility(true);
    assert.equal(env.timers.size, 0);
    await env.advance(120000);
    assert.deepEqual(times, [2000]);
    env.visibility(false);
    env.visibility(false);
    assert.deepEqual(env.delays(), [0]);
    await env.advance(0);
    assert.deepEqual(times, [2000, 122000]);
    assert.deepEqual(env.delays(), [8000], 'restoring visibility does not reset failed status backoff');
});

test('an in-flight poll cannot overlap on repeated visibility changes', async t => {
    const env = environment();
    t.after(() => env.runtime.dispose());
    const work = deferred();
    let calls = 0;
    env.runtime.startPoll(() => { calls++; return work.promise; }, 2000, { backoff: true });
    await env.advance(2000);
    env.visibility(true);
    env.visibility(false);
    env.visibility(false);
    await env.advance(60000);
    assert.equal(calls, 1);
    assert.equal(env.timers.size, 0);
    work.resolve(false);
    await flush();
    assert.equal(env.timers.size, 1, 'a settled callback leaves only one next retry');
});

for (const stop of ['stop', 'beginPage', 'dispose']) {
    test(`${stop} prevents a late failed callback from restarting polling`, async t => {
        const env = environment();
        t.after(() => env.runtime.dispose());
        const work = deferred();
        let calls = 0;
        const poll = env.runtime.startPoll(() => { calls++; return work.promise; }, 2000, { backoff: true });
        await env.advance(2000);
        if (stop === 'stop') poll.stop();
        else env.runtime[stop]();
        work.resolve(false);
        await flush();
        env.visibility(true);
        env.visibility(false);
        await env.advance(120000);
        assert.equal(calls, 1);
        assert.equal(env.timers.size, 0);
        if (stop === 'dispose') assert.equal(env.listeners.size, 0);
    });
}

test('navigation preserves app-wide polling and its existing failure delay', async t => {
    const env = environment();
    t.after(() => env.runtime.dispose());
    let pageCalls = 0;
    let globalCalls = 0;
    env.runtime.startPoll(async () => { pageCalls++; return false; }, 2000, { backoff: true });
    env.runtime.startPoll(async () => { globalCalls++; return false; }, 2000, { global: true, backoff: true });
    await env.advance(2000);
    env.runtime.beginPage();
    assert.deepEqual(env.delays(), [4000]);
    await env.advance(4000);
    assert.equal(pageCalls, 1);
    assert.equal(globalCalls, 2);
    assert.deepEqual(env.delays(), [8000]);
});
