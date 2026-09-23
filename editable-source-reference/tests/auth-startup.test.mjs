import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import ts from 'typescript';

const apiSource = readFileSync(new URL('../src/api.ts', import.meta.url), 'utf8');
const appSource = readFileSync(new URL('../src/App.vue', import.meta.url), 'utf8');
const transpile = source => ts.transpileModule(source, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext },
}).outputText;
const flush = () => new Promise(resolve => setImmediate(resolve));

async function apiScenario(handler) {
  let now = 0, timerId = 0;
  const timers = new Map(), calls = [], scheduled = [], redirects = [];
  const context = vm.createContext({
    console, URL, URLSearchParams, AbortController, DOMException, Error, TypeError,
    fetch: (url, options) => { calls.push({ url, options }); return handler(url, options); },
    window: {
      setTimeout: (callback, delay) => { const id = ++timerId; scheduled.push(delay); timers.set(id, { callback, at: now + delay }); return id; },
      clearTimeout: id => timers.delete(id),
      location: { assign: url => redirects.push(url) },
    },
  });
  const module = new vm.SourceTextModule(transpile(`${apiSource}\nexport const testRequest = request;`), { context });
  await module.link(() => { throw Error('Only erased type imports are expected'); });
  await module.evaluate();
  return {
    api: module.namespace.api, request: module.namespace.testRequest, timers, calls, scheduled, redirects,
    get now() { return now; },
    async nextTimer() {
      await flush();
      const [id, timer] = [...timers].sort((a, b) => a[1].at - b[1].at)[0] || [];
      assert.ok(timer, 'Expected a request deadline or retry delay');
      timers.delete(id); now = timer.at; timer.callback(); await flush();
    },
  };
}

function untilAborted(signal) {
  return new Promise((_resolve, reject) => {
    const fail = () => reject(new DOMException('Aborted', 'AbortError'));
    if (signal.aborted) fail();
    else signal.addEventListener('abort', fail, { once: true });
  });
}

function response(value, { status = 200, body } = {}) {
  return { ok: status >= 200 && status < 300, status, redirected: false,
    headers: { get: () => 'application/json' }, json: body || (async () => value) };
}

for (const stage of ['headers', 'success-body', 'error-body']) {
  test(`identity verification stops after one 15 second attempt when ${stage} stalls`, async () => {
    const scenario = await apiScenario((_url, { signal }) => stage === 'headers'
      ? untilAborted(signal)
      : Promise.resolve(response(null, { status: stage === 'error-body' ? 503 : 200, body: () => untilAborted(signal) })));
    const pending = scenario.api.me();
    const rejected = assert.rejects(pending, error => error.status === 408 && /15 秒/.test(error.message));
    await scenario.nextTimer(); await rejected;
    assert.equal(scenario.now, 15000);
    assert.equal(scenario.calls.length, 1);
    assert.equal(scenario.calls[0].url, 'api/auth/me');
    assert.equal(scenario.timers.size, 0);
    assert.deepEqual(scenario.redirects, []);
  });
}

test('an identity service 503 or 401 is surfaced without silent retries or logout', async () => {
  for (const status of [503, 401]) {
    const scenario = await apiScenario(async () => response({ detail: 'server decision' }, { status }));
    await assert.rejects(scenario.api.me(), error => error.status === status && error.message === 'server decision');
    assert.equal(scenario.calls.length, 1);
    assert.equal(scenario.timers.size, 0);
    assert.deepEqual(scenario.redirects, []);
  }
});

test('a successful identity response retains actual server permissions and cancels its deadline', async () => {
  const identity = { user: { number: 'FD-TEST-A' }, permissions: { asset_admin: false, reviewer_roles: ['member'] } };
  const scenario = await apiScenario(async () => response(identity));
  assert.deepEqual(await scenario.api.me(), identity);
  assert.deepEqual(scenario.scheduled, [15000]);
  assert.equal(scenario.timers.size, 0);
});

test('other reads retain three 90 second attempts, including body deadlines', async () => {
  const scenario = await apiScenario(async (_url, { signal }) => response(null, { body: () => untilAborted(signal) }));
  let settled = false;
  const pending = scenario.api.stats();
  const rejected = assert.rejects(pending, error => error.status === 408 && error.attempts === 3)
    .then(() => { settled = true; });
  for (let count = 0; !settled && count < 6; count++) await scenario.nextTimer();
  await rejected;
  assert.equal(scenario.calls.length, 3);
  assert.equal(scenario.scheduled.filter(delay => delay === 90000).length, 3);
  assert.equal(scenario.timers.size, 0);
});

test('a write whose response body times out is never automatically replayed', async () => {
  const scenario = await apiScenario(async (_url, { signal }) => response(null, { body: () => untilAborted(signal) }));
  const rejected = assert.rejects(scenario.request('api/uploads/complete', { method: 'POST', body: '{}' }),
    error => error.status === 408);
  await scenario.nextTimer(); await rejected;
  assert.equal(scenario.now, 90000);
  assert.equal(scenario.calls.length, 1);
  assert.equal(scenario.timers.size, 0);
});

test('caller cancellation remains active after headers without replaying or keeping a timer', async () => {
  const scenario = await apiScenario(async (_url, { signal }) => response(null, { body: () => untilAborted(signal) }));
  const controller = new AbortController();
  const rejected = assert.rejects(scenario.request('api/read-only', { signal: controller.signal }), error => error.status === 408);
  await flush();
  assert.equal(scenario.timers.size, 1);
  controller.abort(); await rejected;
  assert.equal(scenario.calls.length, 1);
  assert.equal(scenario.timers.size, 0);
});

const appScript = appSource.match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1];
const appTree = ts.createSourceFile('App.ts', appScript, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS);
const bootSource = appTree.statements.find(statement => ts.isVariableStatement(statement)
  && statement.declarationList.declarations.some(declaration => declaration.name.getText(appTree) === 'boot')).getText(appTree);

async function bootScenario(me, { existingUser = null, existingPermissions = { asset_admin: false }, integratedIntoHub = false } = {}) {
  class ApiError extends Error { constructor(message, status) { super(message); this.status = status; } }
  const state = { user: { value: existingUser }, permissions: { value: existingPermissions }, checking: { value: true },
    bootError: { value: '' }, bootNeedsLogin: { value: false }, syncInfo: { value: null }, view: { value: 'hall' } };
  const counts = { me: 0, loadAll: 0, reviews: 0, logout: 0 };
  const api = { me: () => { counts.me++; return me(ApiError); }, refreshStatus: async () => ({ state: 'idle' }),
    logout: async () => { counts.logout++; } };
  const redirects = [];
  const context = vm.createContext({ ...state, api, ApiError, Error, URLSearchParams, integratedIntoHub, returnToHub: () => redirects.push('hub'),
    openLinkedAsset: async () => {}, // Deep-link behavior is exercised in linked-asset.test.mjs.
    loadAll: async () => { counts.loadAll++; }, loadReviewPendingCount: () => { counts.reviews++; },
    syncOss: () => { throw Error('No sync should be started'); }, window: { location: { search: '' } } });
  const module = new vm.SourceTextModule(transpile(`let bootInFlight = false;\n${bootSource}\nexport {boot};`), { context });
  await module.link(() => { throw Error('Unexpected import'); }); await module.evaluate();
  return { ...state, counts, boot: module.namespace.boot, api, redirects };
}

test('integrated entry returns to hub only on a verified 401, never on temporary errors', async () => {
  for (const status of [401, 403, 408, 503]) {
    const scenario = await bootScenario(ApiError => Promise.reject(new ApiError('identity response', status)), { integratedIntoHub: true });
    await scenario.boot();
    assert.equal(scenario.redirects.length, status === 401 ? 1 : 0);
    assert.equal(scenario.counts.logout, 0);
  }
});

test('an uncertain startup shows a retryable error without discarding the existing user or permissions', async () => {
  const user = { number: 'FD-TEST-A' }, permissions = { asset_admin: false, reviewer_roles: ['member'] };
  const scenario = await bootScenario(ApiError => Promise.reject(new ApiError('15 second timeout', 408)),
    { existingUser: user, existingPermissions: permissions });
  await scenario.boot();
  assert.equal(scenario.checking.value, false);
  assert.match(scenario.bootError.value, /timeout/);
  assert.equal(scenario.bootNeedsLogin.value, false);
  assert.equal(scenario.user.value, user);
  assert.equal(scenario.permissions.value, permissions);
  assert.deepEqual(scenario.counts, { me: 1, loadAll: 0, reviews: 0, logout: 0 });
});

test('repeated retry clicks share one pending verification and enter only after actual identity succeeds', async () => {
  let answer;
  const pending = new Promise(resolve => { answer = resolve; });
  const scenario = await bootScenario(() => pending);
  const first = scenario.boot();
  await scenario.boot();
  assert.equal(scenario.counts.me, 1);
  assert.equal(scenario.user.value, null);
  assert.equal(scenario.counts.loadAll, 0);
  const identity = { user: { number: 'FD-TEST-A' }, permissions: { asset_admin: false, reviewer_roles: ['member'] } };
  answer(identity); await first;
  assert.equal(scenario.user.value, identity.user);
  assert.equal(scenario.permissions.value, identity.permissions);
  assert.equal(scenario.bootError.value, '');
  assert.equal(scenario.checking.value, false);
  assert.deepEqual(scenario.counts, { me: 1, loadAll: 1, reviews: 1, logout: 0 });
});

test('retry after an uncertain response can succeed without a logout or permission substitution', async () => {
  let attempts = 0;
  const identity = { user: { number: 'FD-TEST-A' }, permissions: { asset_admin: false } };
  const scenario = await bootScenario(ApiError => ++attempts === 1
    ? Promise.reject(new ApiError('unavailable', 503)) : Promise.resolve(identity));
  await scenario.boot();
  assert.equal(scenario.user.value, null); assert.ok(scenario.bootError.value);
  await scenario.boot();
  assert.equal(scenario.user.value, identity.user);
  assert.equal(scenario.permissions.value, identity.permissions);
  assert.equal(scenario.bootError.value, '');
  assert.deepEqual(scenario.counts, { me: 2, loadAll: 1, reviews: 1, logout: 0 });
});

test('only a confirmed 401 offers login; malformed identity cannot open the application', async () => {
  const denied = await bootScenario(ApiError => Promise.reject(new ApiError('login required', 401)));
  await denied.boot();
  assert.equal(denied.bootNeedsLogin.value, true);
  assert.equal(denied.user.value, null);
  assert.equal(denied.counts.loadAll, 0);
  for (const identity of [{}, { user: { number: '' }, permissions: {} }, { user: { number: 'FD-TEST-A' }, permissions: [] }]) {
    const malformed = await bootScenario(async () => identity);
    await malformed.boot();
    assert.ok(malformed.bootError.value);
    assert.equal(malformed.user.value, null);
    assert.equal(malformed.bootNeedsLogin.value, false);
    assert.equal(malformed.counts.loadAll, 0);
  }
});
