const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

function setup() {
  const context = vm.createContext({URL});
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../media-evidence.js'), 'utf8'), context);
  return context.GermanMediaEvidence;
}
const movie = id => `https://www.disneyplus.com/de-de/play/${id}`;
const media = path => `https://vod-akc-eu-west-2.media.dssott.com/${'signed'.repeat(59)}/${path}?token=secret`;
function request(tabId, id, doc = movie('one'), name = 'audio/de/pts_840.mp4a') {
  return {tabId, requestId: id, documentUrl: doc, url: media(name), statusCode: 200, timeStamp: 1793370000123,
    responseHeaders: [{name: 'Content-Type', value: 'audio/mp4'}, {name: 'Set-Cookie', value: 'never-forward'}]};
}

test('canonical media paths discard signatures, queries, hosts and irrelevant domains', () => {
  const {canonicalMediaPath} = setup();
  assert.equal(canonicalMediaPath(media('ps01/disney/asset/audio/de.m3u8')), '/ps01/disney/asset/audio/de.m3u8');
  assert.equal(canonicalMediaPath('https://example.com/media.mp4a?token=secret'), null);
  assert.equal(canonicalMediaPath('http://vod.media.dssott.com/media.mp4a'), null);
});

test('successful audio and playlist metadata remains strictly scoped to its own tab', () => {
  const {MediaEvidence} = setup();
  const evidence = new MediaEvidence();
  const one = request(1, 'one');
  const two = request(2, 'two', movie('two'), 'audio/de/other.mp4a');
  evidence.begin(one); evidence.complete(one);
  evidence.begin(two); evidence.complete(two);
  const first = evidence.forTab(1, movie('one'));
  assert.equal(first.length, 1);
  assert.equal(first[0].path, '/audio/de/pts_840.mp4a');
  assert.equal(first[0].observedAt, 1793370000123);
  assert.ok(!JSON.stringify(first).includes('secret'));
  assert.ok(!JSON.stringify(first).includes('never-forward'));
  assert.equal(evidence.forTab(2, movie('two'))[0].path, '/audio/de/other.mp4a');
  for (const [id, type, status] of [['video','video/mp4',200], ['fail','audio/mp4',404], ['image','image/png',200]]) {
    const details = {...request(1, id), statusCode: status, responseHeaders: [{name: 'content-type', value: type}]};
    evidence.begin(details); evidence.complete(details);
  }
  assert.equal(evidence.forTab(1, movie('one')).length, 1);
});

test('full navigation and SPA movie changes clear evidence and reject late old requests', () => {
  const {MediaEvidence} = setup();
  const evidence = new MediaEvidence();
  const old = request(1, 'old');
  evidence.begin(old); evidence.complete(old);
  const pending = request(1, 'pending');
  evidence.begin(pending);
  evidence.navigate(1, movie('one'), true);
  evidence.complete(pending);
  assert.equal(evidence.forTab(1, movie('one')).length, 0);
  const refreshed = request(1, 'new');
  evidence.begin(refreshed); evidence.complete(refreshed);
  assert.equal(evidence.forTab(1, movie('one')).length, 1);
  assert.equal(evidence.forTab(1, movie('two')).length, 0);
  evidence.complete(old); // A late old completion has no current-generation start.
  assert.equal(evidence.forTab(1, movie('two')).length, 0);
});

test('authoritative SPA tab identity wins over stale webRequest documentUrl', () => {
  const {MediaEvidence} = setup();
  const evidence = new MediaEvidence();
  evidence.navigate(1, movie('old'));
  const oldPending = request(1, 'old-pending', movie('old'), 'audio/de/old.mp4a');
  evidence.begin(oldPending);
  evidence.navigate(1, movie('new'));
  evidence.complete(oldPending);
  assert.equal(evidence.forTab(1, movie('new')).length, 0);
  const newRequest = request(1, 'new-request', movie('old'), 'audio/de/new.mp4a');
  evidence.begin(newRequest); evidence.complete(newRequest);
  const records = evidence.forTab(1, movie('new'));
  assert.equal(records.length, 1);
  assert.equal(records[0].path, '/audio/de/new.mp4a');
});

test('a changed title on the same route clears old evidence; pause and rewind do not', () => {
  const {MediaEvidence} = setup();
  const evidence = new MediaEvidence();
  evidence.navigate(1, movie('one'), false, 'Old movie');
  const old = request(1, 'old'); evidence.begin(old); evidence.complete(old);
  assert.equal(evidence.forTab(1, movie('one'), 'Old movie').length, 1);
  assert.equal(evidence.forTab(1, movie('one'), 'Old movie').length, 1);
  assert.equal(evidence.forTab(1, movie('one'), 'New movie').length, 0);
});

test('evidence is bounded to the latest128 canonical paths without changing path contents', () => {
  const {MediaEvidence} = setup();
  const evidence = new MediaEvidence();
  for (let i = 0; i < 150; i++) {
    const item = request(1, String(i), movie('one'), `audio/de/${i}.mp4a`);
    evidence.begin(item); evidence.complete(item);
  }
  const result = evidence.forTab(1, movie('one'));
  assert.equal(result.length, 128);
  assert.equal(result[0].path, '/audio/de/22.mp4a');
  assert.equal(result.at(-1).path, '/audio/de/149.mp4a');
  evidence.remove(1);
  assert.equal(evidence.forTab(1, movie('one')).length, 0);
});
