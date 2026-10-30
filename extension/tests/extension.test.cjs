const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

function loadCore() {
  const context = vm.createContext({});
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../subtitle-core.js'), 'utf8'), context);
  return context.CocoSubtitleCore;
}

const TOKEN = 'Test_ONLY_aB12-Code_0123456789abcdefghijklm';
function background({enabled = true, fetchImpl, token = TOKEN} = {}) {
  let listener;
  const calls = [];
  const saved = [];
  const events = {};
  const currentTab = {id: 7, url: 'https://www.disneyplus.com/de-de/play/movie?private=context', title: 'Coco | Disney+'};
  const event = name => ({addListener(fn) { events[name] = fn; }});
  const browser = {
    runtime: {
      id: 'test@local',
      getURL: file => `moz-extension://test/${file}`,
      onMessage: {addListener(fn) { listener = fn; }},
      onInstalled: {addListener() {}}, onStartup: {addListener() {}}
    },
    storage: {local: {async get() { return {enabled, serviceToken: token}; }, async set(value) { saved.push(value); }}},
    browserAction: {setBadgeText() {}, setBadgeBackgroundColor() {}, setTitle() {}},
    tabs: {async query() { return []; }, async sendMessage() {}, async get() { return {...currentTab}; },
      onUpdated: event('updated'), onRemoved: event('removed')},
    webRequest: {onBeforeRequest: event('before'), onCompleted: event('completed'), onErrorOccurred: event('error')}
  };
  const fetch = async (url, options) => {
    calls.push({url, options});
    if (fetchImpl) return fetchImpl(url, options);
    return {ok: true, async json() {
      return {cues: [{start: 840, end: 842, text: 'Hallo.'}], status: {state: 'ready'}, token: 'must-not-leak'};
    }};
  };
  const context = vm.createContext({
    browser, fetch, URL, AbortController, setTimeout, clearTimeout
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../subtitle-core.js'), 'utf8'), context);
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../media-evidence.js'), 'utf8'), context);
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../background.js'), 'utf8'), context);
  const sender = {id: browser.runtime.id, url: 'https://www.disneyplus.com/de-de/play/movie?private=context', tab: {id: 7}, frameId: 0};
  const message = {type: 'COCO_SYNC', currentTime: 840, paused: false, playbackRate: 1, duration: 6300, title: 'Coco | Disney+'};
  return {listener, calls, sender, message, currentTab, events, saved};
}

test('captions use actual movie time, including exact boundaries and backwards seeks', () => {
  const core = loadCore();
  const cues = core.normalizeCues([
    {start: 842, end: 845, text: 'Zwei.'},
    {start: 840, end: 842, text: 'Eins.'},
    {start: 841, end: 842, text: 'Eins.'},
    {start: 842, end: 844, text: 'Antwort.'}
  ]);
  assert.equal(core.captionAt(cues, 839.99), '');
  assert.equal(core.captionAt(cues, 840), 'Eins.');
  assert.equal(core.captionAt(cues, 841.5), 'Eins.');
  assert.equal(core.captionAt(cues, 842), 'Antwort.\nZwei.');
  assert.equal(core.captionAt(cues, 845), '');
  assert.equal(core.captionAt(cues, 840.2), 'Eins.');
});

test('invalid and empty cues never render, but text is preserved without HTML interpretation', () => {
  const core = loadCore();
  const cues = core.normalizeCues([
    {start: 0, end: 1, text: '   '}, {start: NaN, end: 2, text: 'bad'},
    {start: 4, end: 2, text: 'bad'}, {start: -1, end: 1, text: 'bad'},
    {start: 0, end: 1, text: '<img src=x onerror=bad()> & Grüße'}
  ]);
  assert.equal(cues.length, 1);
  assert.equal(core.captionAt(cues, 0.5), '<img src=x onerror=bad()> & Grüße');
});

test('media diagnostics are bounded and do not forward source URLs or arbitrary fields', () => {
  const core = loadCore();
  const diagnostics = core.sanitizeDiagnostics({
    currentSrc: 'https://media.example/private-token', currentSrcType: 'blob',
    videoCount: 2, selectedVideoIndex: 1,
    buffered: [[800, 842.1234], [Infinity, 845]], seekable: [[0, 6300]],
    controls: Array.from({length: 10}, () => ({label: 'x'.repeat(1000), valueText: '13:58', secret: 'must-not-leak'})),
    times: Array.from({length: 15}, () => ({tag: 'span', className: 'a'.repeat(200), text: '13:58'}))
  });
  const json = JSON.stringify(diagnostics);
  assert.ok(json.length <= 4096);
  assert.ok(!json.includes('private-token'));
  assert.ok(!json.includes('must-not-leak'));
  assert.equal(diagnostics.buffered.length, 1);
  assert.equal(diagnostics.buffered[0][1], 842.1234);
});

test('only this extension on Disney can send playback to the local service', async () => {
  const {listener, calls, sender, message} = background();
  assert.equal(listener(message, {...sender, url: 'https://disneyplus.com.attacker.example/play'}), undefined);
  assert.equal(listener(message, {...sender, id: 'another-extension'}), undefined);
  assert.equal(listener(message, {...sender, url: 'http://www.disneyplus.com/play'}), undefined);
  assert.equal(listener(message, {...sender, tab: undefined}), undefined);
  assert.equal(calls.length, 0);
  const result = await listener({...message, pageUrl: 'https://attacker.example'}, sender);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, 'http://127.0.0.1:8766/sync');
  assert.equal(calls[0].options.headers.Authorization, `Bearer ${TOKEN}`);
  assert.equal(JSON.parse(calls[0].options.body).pageUrl, 'https://www.disneyplus.com/de-de/play/movie');
  assert.equal(JSON.stringify(result).includes(TOKEN), false);
  assert.equal(JSON.stringify(result).includes('must-not-leak'), false);
});

test('turning captions off stops requests that could trigger paid transcription', async () => {
  const {listener, calls, sender, message} = background({enabled: false});
  const result = await listener(message, sender);
  assert.equal(result.enabled, false);
  assert.equal(calls.length, 0);
});

test('only the actual popup can clear the billing retry latch', async () => {
  const {listener, calls, sender} = background();
  assert.equal(listener({type: 'COCO_RETRY'}, sender), undefined);
  assert.equal(calls.length, 0);
  const result = await listener({type: 'COCO_RETRY'}, {id: 'test@local', url: 'moz-extension://test/popup.html'});
  assert.equal(calls[0].url, 'http://127.0.0.1:8766/retry');
  assert.equal(result.status.state, 'preparing');
});

test('parallel requests do not queue obsolete playback positions', async () => {
  let release;
  const {listener, calls, sender, message} = background({fetchImpl: () => new Promise(resolve => { release = resolve; })});
  const first = listener(message, sender);
  await new Promise(resolve => setImmediate(resolve));
  const second = await listener({...message, currentTime: 1600}, sender);
  assert.equal(second.busy, true);
  assert.equal(calls.length, 1);
  release({ok: true, async json() { return {cues: [], status: {state: 'ready'}}; }});
  await first;
});

test('SPA navigation uses the current tab URL and title instead of stale sender.url', async () => {
  const {listener, calls, sender, message, currentTab} = background();
  currentTab.url = 'https://www.disneyplus.com/de-de/play/new-movie?context=secret';
  currentTab.title = 'A different movie | Disney+';
  await listener(message, sender);
  const body = JSON.parse(calls[0].options.body);
  assert.equal(body.pageUrl, 'https://www.disneyplus.com/de-de/play/new-movie');
  assert.equal(body.title, currentTab.title);
  assert.ok(Array.isArray(body.diagnostics.mediaRequests));
});

test('a movie switch while transcription sync is pending discards the old response', async () => {
  let release;
  const {listener, sender, message, currentTab} = background({fetchImpl: () => new Promise(resolve => { release = resolve; })});
  const pending = listener(message, sender);
  await new Promise(resolve => setImmediate(resolve));
  currentTab.url = 'https://www.disneyplus.com/de-de/play/new-movie';
  release({ok: true, async json() { return {cues: [{start: 0, end: 9999, text: 'Wrong movie'}], status: {state: 'ready'}}; }});
  const result = await pending;
  assert.equal(result.stale, true);
  assert.equal(result.cues.length, 0);
});

test('pairing is popup-only, validates a case-sensitive base64url code and never returns it', async () => {
  const {listener, calls, saved, sender, message} = background({token: ''});
  const unpaired = await listener(message, sender);
  assert.equal(unpaired.status.state, 'pairing');
  assert.equal(calls.length, 0);
  assert.equal(listener({type: 'GERMAN_PAIR_SERVICE', code: TOKEN}, sender), undefined);
  const popup = {id: 'test@local', url: 'moz-extension://test/popup.html'};
  const invalid = await listener({type: 'GERMAN_PAIR_SERVICE', code: 'bad code'}, popup);
  assert.ok(invalid.pairingError);
  assert.equal(calls.length, 0);
  const result = await listener({type: 'GERMAN_PAIR_SERVICE', code: ` ${TOKEN} `}, popup);
  assert.equal(calls[0].url, 'http://127.0.0.1:8766/status');
  assert.equal(calls[0].options.headers.Authorization, `Bearer ${TOKEN}`);
  assert.equal(saved[0].serviceToken, TOKEN);
  assert.equal(result.paired, true);
  assert.equal(JSON.stringify(result).includes(TOKEN), false);
  await listener(message, sender);
  assert.equal(calls[1].options.headers.Authorization, `Bearer ${TOKEN}`);
});

test('rejected pairing never overwrites a working stored token', async () => {
  const {listener, saved} = background({fetchImpl: async () => ({ok: false, status: 401})});
  const result = await listener({type: 'GERMAN_PAIR_SERVICE', code: 'X'.repeat(43)},
    {id: 'test@local', url: 'moz-extension://test/popup.html'});
  assert.ok(result.pairingError);
  assert.equal(result.paired, true);
  assert.equal(saved.length, 0);
});
