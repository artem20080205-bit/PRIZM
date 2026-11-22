'use strict';

const assert = require('node:assert/strict');
const { randomBytes, randomUUID, createHash } = require('node:crypto');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const { DatabaseSync } = require('node:sqlite');
const { test } = require('node:test');
const { createPrismServer } = require('../src/server.cjs');
const { Client, TransferState, invite, parseInvite } = require('../src/client.cjs');

const hash = data => createHash('sha256').update(data).digest('hex');
const quiet = () => {};
const active = () => new AbortController().signal;

async function setup(t, old = false) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'prism-'));
  const folder = path.join(root, 'server');
  await fs.mkdir(folder);
  let oldFile;
  if (old) {
    const users = ['Хозяин', 'Друг'].map(name => ({ name, token: randomBytes(32).toString('base64url') }));
    await fs.writeFile(path.join(folder, 'access.json'), JSON.stringify({ users }));
    const db = new DatabaseSync(path.join(folder, 'server.sqlite'));
    db.exec(`
      CREATE TABLE messages(id INTEGER PRIMARY KEY,sender INTEGER NOT NULL,client_id TEXT NOT NULL,text TEXT NOT NULL,file_id TEXT,created REAL NOT NULL,UNIQUE(sender,client_id));
      CREATE TABLE uploads(id TEXT PRIMARY KEY,owner INTEGER NOT NULL,name TEXT NOT NULL,size INTEGER NOT NULL,sha256 TEXT NOT NULL,finished INTEGER DEFAULT 0);
      INSERT INTO messages VALUES(12,0,'legacy','Старая переписка',NULL,1);
    `);
    oldFile = randomUUID();
    await fs.mkdir(path.join(folder, 'files'));
    await fs.writeFile(path.join(folder, 'files', oldFile), 'abc');
    db.prepare('INSERT INTO uploads VALUES(?,1,?,?,?,1)').run(oldFile, 'old.txt', 3, hash('abc'));
    db.prepare("INSERT INTO messages VALUES(13,1,'legacy-file','',?,1)").run(oldFile);
    db.close();
  }
  const server = createPrismServer(folder);
  const url = await server.listen(0);
  const state = new TransferState(path.join(root, 'client_state.sqlite'));
  const [host, guest] = server.users.map(({ token }) => new Client({ url, token }, state));
  // Register cleanup in a single hook so handles close before directory removal on Windows.
  t.after(async () => {
    await server.close();
    state.close();
    await fs.rm(root, { recursive: true, force: true });
  });
  return { server, root, folder, state, url, host, guest, oldFile };
}

test('old SQLite messages, attachments and invitations work with the Node server', async t => {
  const { guest, host, url, oldFile } = await setup(t, true);
  const code = 'lantern:' + Buffer.from(JSON.stringify({ url, token: guest.token })).toString('base64');
  assert.deepEqual(parseInvite(code), { url, token: guest.token });
  assert.deepEqual(parseInvite(invite(url, guest.token)), { url, token: guest.token });
  const padded = invite('https://example.org', 'a'.repeat(24)).split(':')[1];
  assert.match(padded, /=+$/);
  assert.equal(padded.length % 4, 0);
  assert.throws(() => parseInvite(invite('http://example.org', guest.token)), /HTTPS/);
  assert.throws(() => parseInvite(invite('https://example.org/?', guest.token)), /приглашение/);
  assert.equal((await host.call('GET', '/api/me')).name, 'Хозяин');
  const rows = await guest.call('GET', '/api/messages');
  assert.equal(rows[0].text, 'Старая переписка');
  assert.equal(rows[1].file.name, 'old.txt');
  const response = await guest.request('GET', '/api/files/' + oldFile);
  assert.equal(await response.text(), 'abc');
  const json = { client_id: randomUUID(), text: 'Новое' };
  const first = await guest.call('POST', '/api/messages', { json });
  const retry = await guest.call('POST', '/api/messages', { json });
  assert.ok(first.id > 13);
  assert.equal(first.id, retry.id);
});

test('guest sends and host downloads after a chunk reply is lost', async t => {
  const { host, guest, root } = await setup(t);
  const input = path.join(root, 'случайные данные.bin');
  const data = randomBytes(820000);
  await fs.writeFile(input, data);
  let lost = false;
  let recovered = false;
  guest.fetch = async (url, options) => {
    const result = await fetch(url, options);
    if (!lost && options.method === 'PUT') {
      await result.arrayBuffer();
      lost = true;
      throw new TypeError('Соединение разорвано после записи блока');
    }
    if (lost && options.method === 'POST' && url.pathname === '/api/uploads') recovered = true;
    return result;
  };
  assert.equal(await guest.upload(input, 'general', active(), quiet), 'Файл отправлен');
  assert.ok(lost && recovered);
  const [row] = await host.call('GET', '/api/messages');
  assert.equal(row.sender, 1);
  assert.equal(row.file.sha256, hash(data));
  const output = path.join(root, 'download.bin');
  // Simulate resuming a partial download left by the Python version.
  await fs.writeFile(output + '.' + row.file.id + '.part', data.subarray(0, 77777));
  await host.download(row.file, output, active(), quiet);
  assert.deepEqual(await fs.readFile(output), data);
  await assert.rejects(fs.stat(output + '.' + row.file.id + '.part'), { code: 'ENOENT' });
});

test('cleanup is scoped to a chat and limited to the host', async t => {
  const { root, folder, host, guest } = await setup(t);
  const room = await host.call('POST', '/api/chats', { json: { title: 'Проект' } });
  await assert.rejects(guest.call('POST', '/api/storage/clear', { json: { mode: 'all' } }), { status: 403 });
  await assert.rejects(guest.call('POST', '/api/chats', { json: { title: 'Чужой' } }), { status: 403 });
  const input = path.join(root, 'input.txt');
  await fs.writeFile(input, 'keep');
  await guest.upload(input, room.id, active(), quiet);
  const [row] = await host.call('GET', '/api/messages', { query: { chat_id: room.id } });
  const transfer_id = randomUUID();
  const json = { name: 'pending', size: 4, sha256: hash('test'), transfer_id };
  await guest.call('POST', '/api/uploads', { json });
  await guest.call('PUT', '/api/uploads/' + transfer_id, { query: { offset: 0 }, bytes: Buffer.from('te') });
  assert.equal((await host.call('GET', '/api/storage')).chats[0].cache_bytes, 2);
  await host.call('POST', '/api/storage/clear', { json: { mode: 'all', chat_id: 'general' } });
  assert.equal(await fs.readFile(path.join(folder, 'files', row.file.id), 'utf8'), 'keep');
  await assert.rejects(fs.stat(path.join(folder, 'files', transfer_id)), { code: 'ENOENT' });
  await assert.rejects(guest.call('POST', '/api/uploads', { json }), { status: 409 });
});

test('cancelled upload is deleted on reconnect, download cancellation removes only .part', async t => {
  const { root, folder, state, guest, host } = await setup(t);
  const input = path.join(root, 'input.bin');
  const data = randomBytes(1024 * 1024);
  await fs.writeFile(input, data);
  const up = new AbortController();
  guest.fetch = (url, options) => {
    if (options.method === 'DELETE') throw new TypeError('Офлайн');
    return fetch(url, options);
  };
  assert.match(await guest.upload(input, 'general', up.signal, text => {
    if (text.startsWith('Отправка')) up.abort();
  }), /после восстановления/);
  const [pending] = state.pending(guest.profile);
  assert.equal((await fs.stat(path.join(folder, 'files', pending.id))).size, 256 * 1024);
  await assert.rejects(host.call('DELETE', '/api/uploads/' + pending.id), { status: 403 });
  guest.fetch = fetch;
  assert.equal(await guest.flushCancellations(), 0);
  await assert.rejects(fs.stat(path.join(folder, 'files', pending.id)), { code: 'ENOENT' });
  assert.deepEqual(await fs.readFile(input), data);

  await guest.upload(input, 'general', active(), quiet);
  const [row] = await host.call('GET', '/api/messages');
  const dest = path.join(root, 'destination.bin');
  const down = new AbortController();
  await fs.writeFile(dest, 'previous copy');
  assert.match(await host.download(row.file, dest, down.signal, text => {
    if (text.startsWith('Скачивание')) down.abort();
  }), /Временная копия удалена/);
  assert.equal(await fs.readFile(dest, 'utf8'), 'previous copy');
  await assert.rejects(fs.stat(dest + '.' + row.file.id + '.part'), { code: 'ENOENT' });
});

test('an empty attachment is transferred and verified', async t => {
  const { root, guest, host } = await setup(t);
  const input = path.join(root, 'empty.txt');
  await fs.writeFile(input, '');
  await guest.upload(input, 'general', active(), quiet);
  const [row] = await host.call('GET', '/api/messages');
  const target = path.join(root, 'saved.txt');
  await host.download(row.file, target, active(), quiet);
  assert.equal((await fs.stat(target)).size, 0);
});
