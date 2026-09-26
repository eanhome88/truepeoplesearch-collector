const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const LocalDashboard = require('../tools/dashboard-runtime.js');

const appSource = fs.readFileSync(path.join(__dirname, '../tools/dashboard-app.js'), 'utf8');

function readFunction(start, next) {
    const from = appSource.indexOf(start);
    const to = appSource.indexOf(next, from + start.length);
    assert.ok(from >= 0 && to > from, `missing function section: ${start}`);
    return appSource.slice(from, to);
}

function textElement(initial = '') {
    let value = initial;
    let writes = 0;
    return {
        get textContent() { return value; },
        set textContent(next) { value = next == null ? '' : String(next); writes++; },
        get writes() { return writes; },
    };
}

test('stable text does not mutate the DOM on repeated status refreshes', () => {
    const element = textElement('Ready');
    for (let i = 0; i < 100; i++) LocalDashboard.setTextIfChanged(element, 'Ready');
    assert.equal(element.textContent, 'Ready');
    assert.equal(element.writes, 0);
});

test('changed text is written once and numeric values compare as DOM strings', () => {
    const element = textElement('0');
    LocalDashboard.setTextIfChanged(element, 0);
    assert.equal(element.writes, 0);
    LocalDashboard.setTextIfChanged(element, 12);
    for (let i = 0; i < 100; i++) LocalDashboard.setTextIfChanged(element, '12');
    assert.equal(element.textContent, '12');
    assert.equal(element.writes, 1);
});

test('the text helper preserves plain text and empty-value behavior', () => {
    const element = textElement();
    Object.defineProperty(element, 'innerHTML', {
        set() { throw new Error('Text updates must not parse HTML'); },
    });
    LocalDashboard.setTextIfChanged(element, '<strong>literal</strong>');
    assert.equal(element.textContent, '<strong>literal</strong>');
    LocalDashboard.setTextIfChanged(element, null);
    assert.equal(element.textContent, '');
    const writesAfterClear = element.writes;
    LocalDashboard.setTextIfChanged(element, undefined);
    assert.equal(element.writes, writesAfterClear);
    assert.doesNotThrow(() => LocalDashboard.setTextIfChanged(null, 'Ready'));
});

test('text updates compare the current DOM instead of remembering a previous node or value', () => {
    const first = textElement();
    LocalDashboard.setTextIfChanged(first, 'Ready');
    first.textContent = 'Changed elsewhere';
    LocalDashboard.setTextIfChanged(first, 'Ready');
    assert.equal(first.textContent, 'Ready');
    assert.equal(first.writes, 3);
    const replacement = textElement();
    LocalDashboard.setTextIfChanged(replacement, 'Ready');
    assert.equal(replacement.textContent, 'Ready');
    assert.equal(replacement.writes, 1);
});

test('dashboard text updates use changed-value behavior and tolerate detached elements', () => {
    const element = textElement('Ready');
    const context = vm.createContext({
        LocalDashboard,
        document: { getElementById: id => id === 'status' ? element : null },
    });
    vm.runInContext(readFunction('function setText(', 'function setDisabled('), context);
    for (let i = 0; i < 100; i++) context.setText('status', 'Ready');
    assert.equal(element.writes, 0);
    context.setText('status', 'Updated');
    assert.equal(element.textContent, 'Updated');
    assert.equal(element.writes, 1);
    assert.doesNotThrow(() => context.setText('detached', 'Updated'));
});

test('render replaces content without synchronously reading layout', () => {
    let content = '';
    let writes = 0;
    let layoutReads = 0;
    const element = {
        get innerHTML() { return content; },
        set innerHTML(value) { content = value; writes++; },
        classList: { remove() {}, add() {}, toggle() {} },
        getBoundingClientRect() { layoutReads++; return {}; },
    };
    for (const key of ['offsetWidth', 'offsetHeight', 'clientWidth', 'clientHeight', 'scrollHeight']) {
        Object.defineProperty(element, key, { get() { layoutReads++; return 100; } });
    }
    const context = vm.createContext({
        document: { getElementById: () => element },
    });
    vm.runInContext(readFunction('function render(', 'function setActive('), context);
    for (let i = 0; i < 100; i++) context.render(`<p>${i}</p>`);
    assert.equal(content, '<p>99</p>');
    assert.equal(writes, 100);
    assert.equal(layoutReads, 0);
});
