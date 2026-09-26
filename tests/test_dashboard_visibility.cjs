const { test } = require('node:test');
const assert = require('node:assert/strict');
const { createRuntime } = require('../tools/dashboard-runtime.js');

function fixture(hidden) {
    const listeners = new Set();
    const document = {
        hidden,
        addEventListener(type, listener) {
            assert.equal(type, 'visibilitychange');
            listeners.add(listener);
        },
        removeEventListener(type, listener) {
            assert.equal(type, 'visibilitychange');
            listeners.delete(listener);
        },
    };
    return {
        document,
        listeners,
        change(hidden) {
            document.hidden = hidden;
            for (const listener of listeners) listener();
        },
    };
}

test('visibility state is synchronized on startup, including an initially hidden page', () => {
    for (const initial of [false, true]) {
        const state = fixture(initial);
        const changes = [];
        const runtime = createRuntime({
            document: state.document,
            onVisibilityChange: hidden => changes.push(hidden),
        });
        assert.deepEqual(changes, [initial]);
        assert.equal(state.listeners.size, 1);
        runtime.dispose();
    }
});

test('one visibility listener controls both visual state and polling suspension', async () => {
    const state = fixture(false);
    const changes = [];
    const timers = new Map();
    let sequence = 0;
    let calls = 0;
    const runtime = createRuntime({
        document: state.document,
        onVisibilityChange: hidden => changes.push(hidden),
        setTimeout: (callback, delay) => {
            const id = ++sequence;
            timers.set(id, { callback, delay });
            return id;
        },
        clearTimeout: id => timers.delete(id),
    });
    runtime.startPoll(() => { calls++; }, 2000, { backoff: true });
    assert.equal(timers.size, 1);
    state.change(true);
    assert.equal(timers.size, 0);
    assert.equal(calls, 0);
    state.change(false);
    assert.equal(timers.size, 1);
    const [id, timer] = [...timers][0];
    assert.equal(timer.delay, 0);
    timers.delete(id);
    await timer.callback();
    assert.equal(calls, 1);
    assert.equal(timers.size, 1);
    assert.deepEqual(changes, [false, true, false]);
    assert.equal(state.listeners.size, 1);
    runtime.dispose();
    assert.equal(timers.size, 0);
});

test('disposed pages no longer receive visibility updates', () => {
    const state = fixture(false);
    const changes = [];
    const runtime = createRuntime({
        document: state.document,
        onVisibilityChange: hidden => changes.push(hidden),
    });
    runtime.dispose();
    state.change(true);
    state.change(false);
    assert.deepEqual(changes, [false]);
    assert.equal(state.listeners.size, 0);
});
