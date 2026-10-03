const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '..', 'tools/dashboard-app.js'), 'utf8');
const begin = source.indexOf('function batch100ProgressSummary(');
const end = source.indexOf('let proxyTesting = false;', begin);
assert.ok(begin >= 0 && end > begin);
const context = vm.createContext({ fmt: value => String(value) });
vm.runInContext(source.slice(begin, end), context);

test('sample check shows attempts separately from verified and newly added rows', () => {
    const summary = context.batch100ProgressSummary({
        current: 5, total: 100, verified_present: 3, new_rows: 1,
    });
    assert.match(summary, /已检查 5 \/ 100/);
    assert.match(summary, /数据库可查 3/);
    assert.match(summary, /检查期间新出现 1/);
    assert.doesNotMatch(summary, /成功入库 5/);
});

test('rate-limited sample check is never labelled completed', () => {
    assert.equal(context.batch100FinishLabel({ job_status: 'verification_required' }), '外部任务需人工核查');
    assert.equal(context.batch100FinishLabel({ job_status: 'rate_limited' }), '目标限流，已停止');
    assert.equal(context.batch100FinishLabel({ job_status: 'partial' }), '部分完成，未达到验证目标');
    assert.equal(context.batch100FinishLabel({ job_status: 'completed', new_rows: 0 }), '样本检查结束，无新增');
    assert.equal(context.batch100HasNewRows({ job_status: 'completed', new_rows: 0 }), false);
    assert.equal(context.batch100HasNewRows({ job_status: 'completed', new_rows: 2 }), true);
});
