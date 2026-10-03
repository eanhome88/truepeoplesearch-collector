const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../tools/dashboard-app.js'), 'utf8');
const start = source.indexOf('    async function applyUpdate() {');
const end = source.indexOf('    // 事件绑定', start);
assert.ok(start > 0 && end > start);

async function update(result, { ok = true, error = null } = {}) {
    const requests = [];
    const context = vm.createContext({
        customerReleaseMode: false, isUpgrading: false, confirm: () => true,
        modalUpgradeBtn: {}, modalCancelBtn: {}, modalCheckBtn: {},
        updateTerminal: { style: {} }, terminalLogs: { textContent: '' },
        fetch: async (url, options) => {
            requests.push({ url, options });
            if (error) throw error;
            return { ok, json: async () => result };
        },
        setTimeout: () => { throw new Error('unverified reload forbidden'); },
    });
    vm.runInContext(source.slice(start, end), context);
    await context.applyUpdate();
    return { context, requests };
}

test('update preserves WIP and does not claim a manual restart already happened', async () => {
    const { context, requests } = await update({ ok: true, restart_required: true, logs: [] });
    assert.equal(requests.length, 1);
    assert.equal(JSON.parse(requests[0].options.body).force_stash, false);
    assert.equal(context.modalUpgradeBtn.textContent, '待手动重启');
    assert.equal(context.modalUpgradeBtn.disabled, true);
    assert.equal(context.modalCancelBtn.disabled, false);
    assert.equal(context.isUpgrading, false);
    assert.match(context.terminalLogs.textContent, /尚需手动重启/);
    assert.doesNotMatch(context.terminalLogs.textContent, /系统服务已重载|自动刷新/);
});

test('successful restart instruction is not described as verified service readiness', async () => {
    const { context } = await update({ ok: true, logs: [] });
    assert.equal(context.modalUpgradeBtn.textContent, '待验证运行状态');
    assert.match(context.terminalLogs.textContent, /实际运行版本仍需检查/);
});

test('partial update failure states that changes were not rolled back', async () => {
    const { context } = await update({ ok: false, code_updated: true,
        error: '依赖阶段失败', logs: [] }, { ok: false });
    assert.match(context.terminalLogs.textContent, /代码阶段已执行/);
    assert.match(context.terminalLogs.textContent, /不代表已回滚/);
    assert.equal(context.modalUpgradeBtn.disabled, false);
});

test('lost update response has unknown outcome and is never automatically retried', async () => {
    const { context, requests } = await update(null, { error: new Error('connection lost') });
    assert.equal(requests.length, 1);
    assert.match(context.terminalLogs.textContent, /操作结果未知/);
    assert.equal(context.modalUpgradeBtn.textContent, '结果待确认');
    assert.equal(context.modalUpgradeBtn.disabled, true);
});
