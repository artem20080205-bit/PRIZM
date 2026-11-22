'use strict';

const { createServer } = require('node:http');
const { DatabaseSync } = require('node:sqlite');
const { randomBytes, randomUUID, timingSafeEqual } = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const { pipeline } = require('node:stream/promises');
const { HttpError, UUID, SHA256, hashFile } = require('./shared.cjs');

const CHUNK = 4 * 1024 * 1024;
const requireValue = (condition, status, message) => {
  if (!condition) throw new HttpError(status, message);
};

function configure(root) {
  fs.mkdirSync(root, { recursive: true });
  const filename = path.join(root, 'access.json');
  if (!fs.existsSync(filename)) {
    const users = [1, 2].map(i => ({ name: `Участник ${i}`, token: randomBytes(32).toString('base64url') }));
    fs.writeFileSync(filename, JSON.stringify({ users }, null, 2), { flag: 'wx', mode: 0o600 });
  }
  const { users } = JSON.parse(fs.readFileSync(filename, 'utf8'));
  if (!Array.isArray(users) || users.length < 2 || users.some(user =>
    typeof user?.name !== 'string' || typeof user.token !== 'string' || !/^[\x21-\x7e]{20,256}$/.test(user.token))) {
    throw new Error('Некорректный файл access.json');
  }
  return users;
}

function initDb(filename) {
  const db = new DatabaseSync(filename);
  try {
    db.exec(`
      PRAGMA busy_timeout=5000;
      PRAGMA journal_mode=WAL;
      CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY, sender INTEGER NOT NULL, client_id TEXT NOT NULL,
        text TEXT NOT NULL, file_id TEXT, created REAL NOT NULL, UNIQUE(sender,client_id));
      CREATE TABLE IF NOT EXISTS uploads (
        id TEXT PRIMARY KEY, owner INTEGER NOT NULL, name TEXT NOT NULL,
        size INTEGER NOT NULL, sha256 TEXT NOT NULL, finished INTEGER DEFAULT 0);
    `);
    for (const table of ['messages', 'uploads']) {
      if (!db.prepare(`PRAGMA table_info(${table})`).all().some(column => column.name === 'chat_id')) {
        db.exec(`ALTER TABLE ${table} ADD COLUMN chat_id TEXT NOT NULL DEFAULT 'general'`);
      }
    }
    db.exec(`
      CREATE TABLE IF NOT EXISTS chats(id TEXT PRIMARY KEY,title TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 0);
      INSERT OR IGNORE INTO chats(id,title) VALUES('general','Общий чат');
      CREATE TABLE IF NOT EXISTS counters(name TEXT PRIMARY KEY,value INTEGER NOT NULL);
      INSERT OR IGNORE INTO counters(name,value) SELECT 'messages',COALESCE(MAX(id),0) FROM messages;
      CREATE TABLE IF NOT EXISTS cancelled(id TEXT PRIMARY KEY,owner INTEGER NOT NULL);
    `);
    return db;
  } catch (error) {
    db.close();
    throw error;
  }
}

async function body(req, max) {
  const parts = [];
  let length = 0;
  for await (const part of req.iterator({ destroyOnReturn: false })) {
    length += part.length;
    if (length > max) {
      req.resume();
      throw new HttpError(413, 'Слишком большой запрос');
    }
    parts.push(part);
  }
  return Buffer.concat(parts, length);
}

async function jsonBody(req) {
  const raw = await body(req, 128 * 1024);
  try {
    const value = JSON.parse(raw.toString('utf8'));
    if (value && typeof value === 'object' && !Array.isArray(value)) return value;
  } catch {
    // Invalid JSON uses the same response as other malformed bodies.
  }
  throw new HttpError(422, 'Некорректные данные запроса');
}

function numberParam(url, name, fallback) {
  const raw = url.searchParams.get(name);
  if (raw === null && fallback !== undefined) return fallback;
  const value = Number(raw);
  requireValue(raw && /^-?\d+$/.test(raw) && Number.isSafeInteger(value), 422, 'Некорректное число');
  requireValue(value >= 0, 400, 'Некорректная позиция');
  return value;
}

function uuid(value) {
  requireValue(typeof value === 'string' && UUID.test(value), 422, 'Некорректный идентификатор');
  return value.toLowerCase();
}

function boundedText(value, limit) {
  return typeof value === 'string' && value.length > 0 && [...value].length <= limit;
}

function createPrismServer(folder) {
  const root = path.resolve(folder);
  const users = configure(root);
  const files = path.join(root, 'files');
  fs.mkdirSync(files, { recursive: true });
  const db = initDb(path.join(root, 'server.sqlite'));
  const readers = new Map();
  const finalizers = new Map();
  const get = (sql, ...params) => db.prepare(sql).get(...params);
  const all = (sql, ...params) => db.prepare(sql).all(...params);
  const run = (sql, ...params) => db.prepare(sql).run(...params);
  const filePath = key => path.join(files, uuid(key));
  const fileSize = key => fs.statSync(filePath(key), { throwIfNoEntry: false })?.size;
  const nextId = () => get("UPDATE counters SET value=value+1 WHERE name='messages' RETURNING value").value;
  const revise = key => run('UPDATE chats SET revision=revision+1 WHERE id=?', key);

  function transaction(action) {
    db.exec('BEGIN IMMEDIATE');
    try {
      const result = action();
      db.exec('COMMIT');
      return result;
    } catch (error) {
      db.exec('ROLLBACK');
      throw error;
    }
  }

  function requireChat(id) {
    const chat = get('SELECT * FROM chats WHERE id=?', id);
    requireValue(chat, 404, 'Чат не найден');
    return chat;
  }

  function attachment(id, owner) {
    const row = get('SELECT * FROM uploads WHERE id=?', id);
    requireValue(row && (owner === undefined || row.owner === owner), 404, 'Файл не найден');
    return row;
  }

  function message(row) {
    const result = { ...row, sender_name: users[row.sender]?.name ?? `Участник ${row.sender + 1}` };
    if (row.file_id) result.file = get('SELECT id,name,size,sha256 FROM uploads WHERE id=?', row.file_id) ?? null;
    return result;
  }

  function removeFiles(rows) {
    const busy = rows.filter(row => finalizers.has(row.id));
    for (const { id } of busy) finalizers.get(id).abort();
    requireValue(!busy.length, 409, 'Отменяем проверку файла. Повторите действие через несколько секунд.');
    requireValue(!rows.some(row => readers.has(row.id)), 409, 'Файл сейчас скачивается. Повторите действие после передачи.');
    const targets = rows.map(row => filePath(row.id));
    for (const target of targets) fs.rmSync(target, { force: true });
  }

  function removeUpload(row) {
    run('INSERT OR IGNORE INTO cancelled VALUES(?,?)', row.id, row.owner);
    run('DELETE FROM uploads WHERE id=?', row.id);
  }

  async function route(req, res) {
    const url = new URL(req.url, 'http://127.0.0.1');
    const token = Buffer.from(/^Bearer (.{1,256})$/.exec(req.headers.authorization ?? '')?.[1] ?? '');
    const user = users.findIndex(entry => {
      const expected = Buffer.from(entry.token);
      return token.length === expected.length && timingSafeEqual(token, expected);
    });
    requireValue(user !== -1, 401, 'Неверное приглашение');

    const { pathname } = url;
    const chatId = url.searchParams.get('chat_id') ?? 'general';
    const endpoint = req.method + ' ' + pathname;
    if (['POST /api/chats', 'GET /api/storage', 'POST /api/storage/clear'].includes(endpoint)) {
      requireValue(user === 0, 403, 'Настройки сервера доступны только хозяину');
    }

    switch (endpoint) {
      case 'GET /api/me':
        return { id: user, name: users[user].name, prototype: true, end_to_end_encryption: false, is_host: user === 0, protocol: 3 };
      case 'GET /api/chats':
        return all('SELECT * FROM chats ORDER BY rowid');
      case 'POST /api/chats': {
        const { title } = await jsonBody(req);
        requireValue(boundedText(title, 80) && title.trim(), 400, 'Введите название чата (до 80 символов)');
        const id = randomUUID();
        run('INSERT INTO chats(id,title) VALUES(?,?)', id, title.trim());
        return requireChat(id);
      }
      case 'GET /api/storage':
        return { folder: root, chats: all('SELECT * FROM chats ORDER BY rowid').map(chat => {
          const stats = { cache_bytes: 0, file_bytes: 0, cache_count: 0, file_count: 0 };
          for (const file of all('SELECT id,finished FROM uploads WHERE chat_id=?', chat.id)) {
            const label = file.finished ? 'file' : 'cache';
            stats[label + '_bytes'] += fileSize(file.id) ?? 0;
            stats[label + '_count']++;
          }
          return { ...chat, ...stats, messages: get('SELECT COUNT(*) AS count FROM messages WHERE chat_id=?', chat.id).count };
        }) };
      case 'POST /api/storage/clear': {
        const { mode, chat_id: target } = await jsonBody(req);
        requireValue(['cache', 'files', 'all'].includes(mode) && (target == null || typeof target === 'string'), 422, 'Некорректный режим очистки');
        const chats = target == null ? all('SELECT id FROM chats') : [requireChat(target)];
        const filter = mode === 'cache' ? ' AND finished=0' : '';
        const rows = chats.flatMap(({ id }) => all('SELECT * FROM uploads WHERE chat_id=?' + filter, id));
        removeFiles(rows);
        return transaction(() => {
          for (const row of rows) {
            run("UPDATE messages SET text='[Вложение удалено хозяином сервера]',file_id=NULL WHERE file_id=?", row.id);
            removeUpload(row);
          }
          for (const { id } of chats) {
            if (mode === 'all') run('DELETE FROM messages WHERE chat_id=?', id);
            revise(id);
          }
          return { ok: true, deleted_files: rows.length };
        });
      }
      case 'GET /api/messages': {
        const after = numberParam(url, 'after', 0);
        requireChat(chatId);
        return all('SELECT * FROM messages WHERE id>? AND chat_id=? ORDER BY id LIMIT 200', after, chatId).map(message);
      }
      case 'POST /api/messages': {
        const { client_id, text } = await jsonBody(req);
        const id = uuid(client_id);
        requireValue(boundedText(text, 16000) && text.trim(), 400, 'Введите сообщение (до 16000 символов)');
        requireChat(chatId);
        return transaction(() => {
          const previous = get('SELECT * FROM messages WHERE sender=? AND client_id=?', user, id);
          if (previous) return message(previous);
          const seq = nextId();
          run('INSERT INTO messages(id,sender,client_id,text,created,chat_id) VALUES(?,?,?,?,?,?)',
            seq, user, id, text, Date.now() / 1000, chatId);
          return message(get('SELECT * FROM messages WHERE id=?', seq));
        });
      }
      case 'POST /api/uploads': {
        const { name, size, sha256, transfer_id } = await jsonBody(req);
        requireValue(boundedText(name, 240) && Number.isSafeInteger(size) && size >= 0
          && typeof sha256 === 'string' && SHA256.test(sha256), 422, 'Некорректное описание файла');
        const filename = name.split(/[\\/]/).at(-1);
        requireValue(filename && filename !== '.' && filename !== '..' && !/[\x00-\x1f]/.test(filename), 400, 'Некорректное имя');
        requireChat(chatId);
        const id = transfer_id == null ? randomUUID() : uuid(transfer_id);
        requireValue(!get('SELECT 1 FROM cancelled WHERE id=?', id), 409, 'Передача отменена');
        const row = transfer_id == null
          ? get('SELECT * FROM uploads WHERE owner=? AND chat_id=? AND name=? AND size=? AND sha256=? ORDER BY finished DESC LIMIT 1', user, chatId, filename, size, sha256)
          : get('SELECT * FROM uploads WHERE id=?', id);
        if (row) {
          requireValue(row.owner === user && row.chat_id === chatId && row.sha256 === sha256 && row.size === size, 409, 'Конфликт передачи');
          const offset = fileSize(row.id);
          requireValue(offset !== undefined, 409, 'Копия файла отсутствует на сервере');
          return { id: row.id, offset, finished: Boolean(row.finished) };
        }
        const target = filePath(id);
        fs.writeFileSync(target, '', { flag: 'wx' });
        try {
          run('INSERT INTO uploads(id,owner,name,size,sha256,chat_id) VALUES(?,?,?,?,?,?)', id, user, filename, size, sha256, chatId);
        } catch (error) {
          fs.rmSync(target, { force: true });
          throw error;
        }
        return { id, offset: 0, finished: false };
      }
    }

    const match = /^\/api\/(uploads|files)\/([^/]+)(\/finish)?$/.exec(pathname);
    requireValue(match, 404, 'Команда не найдена');
    const [, type, fileId, finish] = match;
    const key = uuid(fileId);
    const target = filePath(key);
    if (type === 'uploads' && !finish && req.method === 'DELETE') {
      const row = get('SELECT * FROM uploads WHERE id=?', key);
      const previous = get('SELECT owner FROM cancelled WHERE id=?', key);
      requireValue((!row || row.owner === user) && (!previous || previous.owner === user), 403, 'Нельзя отменять чужую отправку');
      if (row) removeFiles([row]);
      return transaction(() => {
        if (row) {
          run('DELETE FROM messages WHERE file_id=?', key);
          removeUpload(row);
          revise(row.chat_id);
        }
        run('INSERT OR IGNORE INTO cancelled VALUES(?,?)', key, user);
        return { ok: true };
      });
    }
    if (type === 'uploads' && !finish && req.method === 'PUT') {
      const offset = numberParam(url, 'offset');
      const data = await body(req, CHUNK);
      const row = attachment(key, user);
      requireValue(!row.finished && offset === fileSize(key), 409, 'Повторите отправку файла для докачки');
      requireValue(offset + data.length <= row.size, 400, 'Превышен размер файла');
      fs.appendFileSync(target, data);
      return { offset: offset + data.length };
    }
    if (type === 'uploads' && finish && req.method === 'POST') {
      const row = attachment(key, user);
      if (row.finished) return { ok: true };
      requireValue(!finalizers.has(key) && fileSize(key) === row.size, 409, 'Файл ещё не загружен полностью или уже проверяется');
      const controller = new AbortController();
      finalizers.set(key, controller);
      try {
        const digest = await hashFile(target, controller.signal);
        requireValue(digest === row.sha256, 409, 'Контрольная сумма не совпадает');
        return transaction(() => {
          run('UPDATE uploads SET finished=1 WHERE id=?', key);
          run('INSERT OR IGNORE INTO messages(id,sender,client_id,text,file_id,created,chat_id) VALUES(?,?,?,?,?,?,?)',
            nextId(), user, 'file:' + key, '', key, Date.now() / 1000, row.chat_id);
          return { ok: true };
        });
      } catch (error) {
        if (controller.signal.aborted) throw new HttpError(409, 'Проверка файла отменена');
        throw error;
      } finally {
        finalizers.delete(key);
      }
    }
    if (type === 'files' && !finish && req.method === 'GET') {
      const row = attachment(key);
      const offset = numberParam(url, 'offset', 0);
      requireValue(row.finished && fileSize(key) === row.size, 404, 'Файл недоступен');
      requireValue(offset <= row.size, 400, 'Некорректная позиция');
      res.writeHead(200, { 'Content-Type': 'application/octet-stream', 'Content-Length': row.size - offset });
      if (offset === row.size) {
        res.end();
        return;
      }
      readers.set(key, (readers.get(key) ?? 0) + 1);
      try {
        await pipeline(fs.createReadStream(target, { start: offset, end: row.size - 1 }), res);
      } finally {
        if (readers.get(key) === 1) readers.delete(key);
        else readers.set(key, readers.get(key) - 1);
      }
      return;
    }
    throw new HttpError(404, 'Команда не найдена');
  }

  function reply(res, code, value) {
    res.writeHead(code, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' });
    res.end(JSON.stringify(value));
  }

  const requests = new Set();
  const server = createServer((req, res) => {
    const request = route(req, res).then(value => {
      if (!res.headersSent && !res.destroyed) reply(res, 200, value);
    }).catch(error => {
      if (res.destroyed) return;
      if (res.headersSent) return res.destroy();
      if (!(error instanceof HttpError)) console.error(error.message);
      reply(res, error.status ?? 500, { detail: error.status ? error.message : 'Ошибка сервера' });
    });
    requests.add(request);
    void request.finally(() => requests.delete(request));
  });
  server.requestTimeout = 120_000;

  let closing;
  return {
    server, root, users,
    async listen(port = 8765) {
      await new Promise((resolve, reject) => {
        server.once('error', reject);
        server.listen(port, '127.0.0.1', () => {
          server.removeListener('error', reject);
          resolve();
        });
      });
      return 'http://127.0.0.1:' + server.address().port;
    },
    close() {
      closing ??= new Promise(resolve => {
        server.close(resolve);
        server.closeAllConnections();
        for (const controller of finalizers.values()) controller.abort();
      }).then(() => Promise.allSettled(requests)).finally(() => db.close());
      return closing;
    }
  };
}

if (require.main === module) {
  const { parseArgs } = require('node:util');
  (async () => {
    const { values } = parseArgs({ options: { data: { type: 'string', default: 'server_data_v02' }, port: { type: 'string', default: '8765' } } });
    const port = Number(values.port);
    if (!Number.isInteger(port) || port < 1024 || port > 65535) throw new Error('Укажите порт от 1024 до 65535');
    const instance = createPrismServer(values.data);
    try {
      console.log(await instance.listen(port));
      for (const signal of ['SIGINT', 'SIGTERM']) process.once(signal, () => void instance.close());
    } catch (error) {
      await instance.close();
      throw error;
    }
  })().catch(error => {
    console.error(error.message);
    process.exitCode = 1;
  });
}

module.exports = { createPrismServer };
