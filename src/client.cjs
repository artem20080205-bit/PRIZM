'use strict';

const fs = require('node:fs/promises');
const { createWriteStream, renameSync } = require('node:fs');
const { basename, resolve } = require('node:path');
const { createHash, randomUUID } = require('node:crypto');
const { DatabaseSync } = require('node:sqlite');
const { pipeline } = require('node:stream/promises');
const { Transform } = require('node:stream');
const { setTimeout: delay } = require('node:timers/promises');
const { UUID, SHA256, HttpError, hashFile } = require('./shared.cjs');

const sha = value => createHash('sha256').update(value).digest('hex');
const retryable = error => error instanceof TypeError || error.name === 'TimeoutError'
  || [408, 429, 500, 502, 503, 504].includes(error.status);

function invite(url, token) {
  const encoded = Buffer.from(JSON.stringify({ url, token })).toString('base64url');
  // The Python client requires Base64 padding when the length is not a multiple of four.
  return 'prism:' + encoded.padEnd(Math.ceil(encoded.length / 4) * 4, '=');
}

function parseInvite(value) {
  const match = /^(?:prism|lantern):([a-z0-9_=-]+)$/i.exec(value.trim());
  if (!match || value.length > 4096) throw new Error('Некорректное приглашение');
  let url, token;
  try {
    const data = JSON.parse(Buffer.from(match[1], 'base64url').toString('utf8'));
    url = new URL(data.url);
    token = data.token;
    if (!/^https?:\/\/[^/?#\\]+\/?$/i.test(data.url) || url.username || url.password
      || typeof token !== 'string' || !/^[\x21-\x7e]{20,256}$/.test(token)) throw new Error();
  } catch {
    throw new Error('Некорректное приглашение');
  }
  if (url.protocol === 'http:' && !/^(localhost|\[::1\]|(?:127\.|10\.|192\.168\.|172\.(?:1[6-9]|2\d|3[01])\.)\d+(?:\.\d+){1,2})$/.test(url.hostname)) {
    throw new Error('Для подключения через интернет требуется HTTPS');
  }
  return { url: url.origin, token };
}

class TransferState {
  constructor(filename) {
    this.db = new DatabaseSync(filename);
    this.db.exec(`
      PRAGMA busy_timeout=5000;
      CREATE TABLE IF NOT EXISTS sessions(profile TEXT,signature TEXT,id TEXT,PRIMARY KEY(profile,signature));
      CREATE TABLE IF NOT EXISTS cancellations(profile TEXT,id TEXT,PRIMARY KEY(profile,id));
    `);
  }

  session(profile, signature) {
    this.db.prepare('INSERT OR IGNORE INTO sessions VALUES(?,?,?)').run(profile, signature, randomUUID());
    return this.db.prepare('SELECT id FROM sessions WHERE profile=? AND signature=?').get(profile, signature).id;
  }

  forget(profile, id) {
    this.db.prepare('DELETE FROM sessions WHERE profile=? AND id=?').run(profile, id);
  }

  cancel(profile, id) {
    this.db.prepare('INSERT OR IGNORE INTO cancellations VALUES(?,?)').run(profile, id);
    this.forget(profile, id);
  }

  pending(profile) {
    return this.db.prepare('SELECT id FROM cancellations WHERE profile=? ORDER BY rowid').all(profile);
  }

  acknowledge(profile, id) {
    this.db.prepare('DELETE FROM cancellations WHERE profile=? AND id=?').run(profile, id);
  }

  close() {
    this.db.close();
  }
}

class Client {
  constructor({ url, token }, state, transport = fetch) {
    this.url = url;
    this.token = token;
    this.profile = sha(token);
    this.state = state;
    this.fetch = transport;
  }

  async request(method, route, { query, json, bytes, signal, timeout = 120_000 } = {}) {
    const url = new URL(route, this.url);
    url.search = new URLSearchParams(query);
    const deadline = AbortSignal.timeout(timeout);
    const response = await this.fetch(url, {
      method, redirect: 'error', signal: signal ? AbortSignal.any([signal, deadline]) : deadline,
      headers: { Authorization: 'Bearer ' + this.token, ...(json ? { 'Content-Type': 'application/json' } : {}) },
      body: json ? JSON.stringify(json) : bytes
    });
    if (!response.ok) {
      const detail = await response.json().catch(() => ({}));
      throw new HttpError(response.status, typeof detail.detail === 'string' ? detail.detail : `Сервер: ${response.status}`);
    }
    return response;
  }

  async call(method, route, options) {
    return (await this.request(method, route, options)).json();
  }

  async flushCancellations() {
    if (this.flushing) return this.flushing;
    this.flushing = (async () => {
      for (const { id } of this.state.pending(this.profile)) {
        try {
          await this.call('DELETE', '/api/uploads/' + id, { timeout: 4000 });
          this.state.acknowledge(this.profile, id);
        } catch (error) {
          if (error.status === 403) this.state.acknowledge(this.profile, id);
          else break;
        }
      }
      return this.state.pending(this.profile).length;
    })();
    try {
      return await this.flushing;
    } finally {
      this.flushing = null;
    }
  }

  async retry(action, signal, report, shouldRetry = retryable) {
    for (let attempt = 0; ; attempt++) {
      signal.throwIfAborted();
      try {
        return await action();
      } catch (error) {
        signal.throwIfAborted();
        if (attempt >= 5 || !shouldRetry(error)) throw error;
        report('Связь прервалась. Восстанавливаем передачу…');
        await delay(Math.min(2 ** attempt, 8) * 1000, undefined, { signal });
      }
    }
  }

  async upload(filename, chatId, signal, report) {
    const before = await fs.stat(filename);
    if (!before.isFile() || !Number.isSafeInteger(before.size)) throw new Error('Выберите обычный файл');
    const query = { chat_id: chatId };
    let id;
    try {
      report('Проверяем файл…');
      const sha256 = await hashFile(filename, signal);
      const signature = sha(chatId + resolve(filename) + before.size + sha256);
      id = this.state.session(this.profile, signature);
      const metadata = { name: basename(filename), size: before.size, sha256, transfer_id: id };
      const status = async () => {
        const result = await this.retry(() => this.call('POST', '/api/uploads', { query, json: metadata, signal }), signal, report);
        if (result.id !== id || !Number.isSafeInteger(result.offset) || result.offset < 0 || result.offset > before.size
          || (result.finished && result.offset !== before.size)) throw new Error('Сервер вернул неверную позицию файла');
        return result.offset;
      };
      let offset = await status();
      const handle = await fs.open(filename, 'r');
      try {
        const buffer = Buffer.alloc(256 * 1024);
        while (offset < before.size) {
          offset = await this.retry(async () => {
            try {
              const { bytesRead } = await handle.read(buffer, 0, Math.min(buffer.length, before.size - offset), offset);
              if (!bytesRead) throw new Error('Исходный файл изменился во время передачи');
              const result = await this.call('PUT', '/api/uploads/' + id, {
                query: { offset }, bytes: buffer.subarray(0, bytesRead), signal
              });
              if (result.offset !== offset + bytesRead) throw new Error('Сервер вернул неверную позицию файла');
              return result.offset;
            } catch (error) {
              signal.throwIfAborted();
              if (!retryable(error) && error.status !== 409) throw error;
              const saved = await status();
              if (saved < offset) throw new Error('Сервер потерял часть файла. Повторите отправку.');
              if (saved === offset) throw error;
              return saved;
            }
          }, signal, report, error => retryable(error) || error.status === 409);
          report(`Отправка: ${Math.round(100 * offset / before.size)}%`);
        }
      } finally {
        await handle.close();
      }
      const after = await fs.stat(filename);
      if (before.size !== after.size || before.mtimeMs !== after.mtimeMs) throw new Error('Файл изменился во время передачи');
      report('Сервер проверяет целостность…');
      await this.retry(() => this.call('POST', `/api/uploads/${id}/finish`, { signal }), signal, report,
        error => retryable(error) || (error.status === 409 && error.message.includes('проверяется')));
      signal.throwIfAborted();
      this.state.forget(this.profile, id);
      return 'Файл отправлен';
    } catch (error) {
      if (!signal.aborted) {
        if (id && error.status === 409 && error.message === 'Передача отменена') this.state.forget(this.profile, id);
        throw error;
      }
      if (!id) return 'Отправка отменена';
      this.state.cancel(this.profile, id);
      await this.flushCancellations();
      return this.state.pending(this.profile).some(row => row.id === id)
        ? 'Отменено. Серверная копия будет удалена после восстановления связи.'
        : 'Отменено. Серверная копия удалена.';
    }
  }

  async download(file, destination, signal, report) {
    const { id, size, sha256 } = file;
    if (typeof id !== 'string' || !UUID.test(id) || !Number.isSafeInteger(size) || size < 0
      || typeof sha256 !== 'string' || !SHA256.test(sha256)) throw new Error('Некорректное вложение');
    const partial = destination + '.' + id + '.part';
    try {
      const offset = await fs.stat(partial).then(stat => stat.size, error => {
        if (error.code === 'ENOENT') return 0;
        throw error;
      });
      if (offset > size) {
        await fs.rm(partial, { force: true });
        throw new Error('Повреждённая временная копия удалена. Повторите скачивание.');
      }
      let received = offset;
      let idleTimer;
      const idle = new AbortController();
      const downloadSignal = AbortSignal.any([signal, idle.signal]);
      const touch = () => {
        clearTimeout(idleTimer);
        idleTimer = setTimeout(() => idle.abort(new Error('Сервер перестал передавать данные')), 120_000);
      };
      const progress = new Transform({
        transform(chunk, _, callback) {
          touch();
          received += chunk.length;
          if (received > size) return callback(new Error('Сервер прислал лишние данные'));
          report(`Скачивание: ${Math.round(100 * received / Math.max(size, 1))}%`);
          callback(null, chunk);
        }
      });
      try {
        touch();
        // An idle deadline allows a steady download to continue regardless of total size.
        const response = await this.request('GET', '/api/files/' + id, { query: { offset }, signal: downloadSignal, timeout: 2 ** 31 - 1 });
        await pipeline(response.body, progress, createWriteStream(partial, { flags: 'a' }),
          { signal: downloadSignal });
      } catch (error) {
        if (idle.signal.aborted) throw idle.signal.reason;
        throw error;
      } finally {
        clearTimeout(idleTimer);
      }
      report('Проверяем скачанный файл…');
      if (received !== size || await hashFile(partial, signal) !== sha256) {
        await fs.rm(partial, { force: true });
        throw new Error('Контрольная сумма не совпала. Временная копия удалена.');
      }
      // Complete the rename in the same turn as the final cancellation check.
      signal.throwIfAborted();
      renameSync(partial, destination);
      return 'Сохранено: ' + destination;
    } catch (error) {
      if (!signal.aborted) throw error;
      await fs.rm(partial, { force: true });
      return 'Скачивание отменено. Временная копия удалена.';
    }
  }
}

module.exports = { Client, TransferState, invite, parseInvite };
