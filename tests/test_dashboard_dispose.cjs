const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

// Count retained runtime resources directly; this avoids depending on when GC runs.
function environment() {
    const collections = [];
    const controllers = [];
    const timers = new Map();
    const listeners = new Map();
    const sends = [];
    let nextTimer = 0;
    class CountedSet extends Set {
        constructor(...args) { super(...args); collections.push(this); }
    }
    class CountedController extends AbortController {
        constructor() { super(); controllers.push(this); }
    }
    const context = { module: { exports: {} }, Set: CountedSet, AbortController: CountedController };
    vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../tools/dashboard-runtime.js'), 'utf8'), context);
    const document = {
        hidden: false,
        addEventListener(name, callback) { listeners.set(name, callback); },
        removeEventListener(name, callback) {
            if (listeners.get(name) === callback) listeners.delete(name);
        },
    };
    const runtime = context.module.exports.createRuntime({
        document,
        fetch: async (...args) => {
            sends.push(args);
            return { ok: true, status: 200, json: async () => ({ synthetic: true }) };
        },
        setTimeout(callback, delay) {
            const id = ++nextTimer;
            timers.set(id, { callback, delay });
            return id;
        },
        clearTimeout(id) { timers.delete(id); },
    });
    return {
        runtime, document, timers, controllers, listeners, sends,
        retained: () => collections.reduce((count, entries) => count + entries.size, 0),
        async fire() {
            const [id, timer] = timers.entries().next().value;
            timers.delete(id);
            await timer.callback();
        },
    };
}

test('live polling, page changes, and explicit stop retain their behavior', async () => {
    const env = environment();
    let calls = 0;
    const local = env.runtime.startPoll(() => { calls++; }, 25);
    const global = env.runtime.startPoll(() => { calls++; }, 50, { global: true });
    assert.equal(local.global, false);
    assert.equal(global.global, true);
    assert.equal(env.timers.size, 2);
    await env.fire();
    assert.equal(calls, 1);
    assert.equal(env.timers.size, 2);
    assert.equal(env.runtime.beginPage(), 1);
    assert.equal(env.controllers.length, 2);
    assert.equal(env.controllers[0].signal.aborted, true);
    assert.equal(env.timers.size, 1);
    assert.equal(env.retained(), 1);
    global.stop();
    global.stop();
    local.stop();
    assert.equal(env.timers.size, 0);
    assert.equal(env.retained(), 0);
    env.runtime.dispose();
    assert.equal(env.listeners.size, 0);
});

test('late poll registrations after disposal do not retain callbacks or allocate timers', () => {
    const env = environment();
    const active = env.runtime.startPoll(() => assert.fail('disposed callback ran'), 25);
    env.runtime.dispose();
    assert.equal(env.retained(), 0);
    for (let index = 0; index < 1000; index++) {
        const config = { global: index % 2 === 0, backoff: true };
        const handle = env.runtime.startPoll(() => assert.fail('late callback ran'), 25, config);
        assert.equal(handle.global, config.global);
        assert.equal(typeof handle.stop, 'function');
        assert.equal(typeof handle.visibility, 'function');
        // A late caller is allowed to drop the handle without ever calling stop.
        handle.visibility();
    }
    assert.equal(env.retained(), 0, 'no late poll is retained by the disposed runtime');
    assert.equal(env.timers.size, 0);
    assert.equal(env.listeners.size, 0);
    active.stop();
    env.runtime.dispose();
});

test('late handles can be stopped repeatedly and remain inactive on visibility calls', () => {
    const env = environment();
    env.runtime.dispose();
    const handle = env.runtime.startPoll(() => assert.fail('late callback ran'), 25);
    for (const hidden of [true, false, true, false]) {
        env.document.hidden = hidden;
        handle.stop();
        handle.visibility();
    }
    assert.equal(env.retained(), 0);
    assert.equal(env.timers.size, 0);
});

test('navigation after disposal does not rebuild page controllers or change the final page', async () => {
    const env = environment();
    const page = env.runtime.beginPage();
    env.runtime.dispose();
    const count = env.controllers.length;
    for (let index = 0; index < 1000; index++) {
        assert.equal(env.runtime.beginPage(), page);
        assert.equal(env.runtime.currentPage(), page);
        assert.equal(env.runtime.isCurrent(page), false);
    }
    assert.equal(env.controllers.length, count);
    assert.equal(env.controllers.at(-1).signal.aborted, true);
    await assert.rejects(env.runtime.request('/synthetic'), { name: 'AbortError' });
    assert.equal(env.sends.length, 0);
    assert.equal(env.timers.size, 0);
    assert.equal(env.retained(), 0);
});

test('disposing while a callback finishes does not schedule a successor or retain a late poll', async () => {
    const env = environment();
    let finish;
    const pending = new Promise(resolve => { finish = resolve; });
    env.runtime.startPoll(async () => {
        await pending;
        env.runtime.startPoll(() => assert.fail('late callback ran'), 25);
    }, 25);
    const running = env.fire();
    env.runtime.dispose();
    finish();
    await running;
    assert.equal(env.retained(), 0);
    assert.equal(env.timers.size, 0);
    assert.equal(env.listeners.size, 0);
});
