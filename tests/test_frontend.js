const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8');

function report(day, totalSeconds = 0) {
  return { from: day, to: day, generated_at: `${day}T12:00:00Z`, total_seconds: totalSeconds, employees: [] };
}

function response(data, ok = true) {
  return { ok, json: async () => data };
}

async function createApp() {
  const nodes = new Map();
  const requests = [];
  function element() {
    return {
      value: '', textContent: '', dataset: {}, hidden: false,
      classList: { toggle() {} },
      replaceChildren() {}, append() {}, addEventListener() {}, setAttribute() {},
    };
  }
  const context = vm.createContext({
    document: {
      querySelector(selector) {
        if (!nodes.has(selector)) nodes.set(selector, element());
        return nodes.get(selector);
      },
      createElement: element,
    },
    URLSearchParams,
    setInterval() {},
    fetch: async url => response(url === '/api/employees'
      ? { employees: [] } : report('2026-11-01')),
  });
  vm.runInContext(source, context);
  await new Promise(setImmediate);
  context.fetch = url => new Promise((resolve, reject) => requests.push({ url, resolve, reject }));
  return {
    nodes,
    requests,
    requestReport(day) {
      nodes.get('#date-from').value = day;
      nodes.get('#date-to').value = day;
      return vm.runInContext('fetchReport()', context);
    },
  };
}

test('a late report response cannot replace the latest report', async () => {
  const app = await createApp();
  const older = app.requestReport('2026-11-01');
  const newer = app.requestReport('2026-11-05');

  app.requests[1].resolve(response(report('2026-11-05', 120)));
  await newer;
  app.requests[0].resolve(response(report('2026-11-01', 60)));
  await older;

  assert.equal(app.nodes.get('#report-period').textContent, '2026-11-05 — 2026-11-05');
  assert.equal(app.nodes.get('#stat-time').textContent, '2m 0s');
});

test('an obsolete failed report request is ignored', async () => {
  const app = await createApp();
  const older = app.requestReport('2026-11-01');
  const newer = app.requestReport('2026-11-05');

  app.requests[1].resolve(response(report('2026-11-05')));
  await newer;
  app.requests[0].reject(new Error('Old network error'));
  await older;

  assert.equal(app.nodes.get('#report-period').textContent, '2026-11-05 — 2026-11-05');
});

test('a failed latest request still reports its error and ignores an older success', async () => {
  const app = await createApp();
  const older = app.requestReport('2026-11-02');
  const newer = app.requestReport('2026-11-05');
  const failure = assert.rejects(newer, /Latest report failed/);

  app.requests[1].resolve(response({ error: 'Latest report failed' }, false));
  await failure;
  app.requests[0].resolve(response(report('2026-11-02')));
  await older;

  assert.equal(app.nodes.get('#report-period').textContent, '2026-11-01 — 2026-11-01');
});
