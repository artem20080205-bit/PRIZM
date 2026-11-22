'use strict';

const { spawn } = require('node:child_process');
const { createWriteStream, existsSync } = require('node:fs');
const fs = require('node:fs/promises');
const path = require('node:path');
const { pipeline } = require('node:stream/promises');
const { Transform } = require('node:stream');
const { setTimeout: delay } = require('node:timers/promises');
const { hashFile } = require('./shared.cjs');

const SIZE = 55397592;
const CHECKSUM = 'd5c1d82031da6253dcdcff64c944cd0fe063016edeb2e4fd088075fc9bd52d0a';
const RELEASE = 'https://github.com/cloudflare/cloudflared/releases/download/2026.11.0/cloudflared-windows-amd64.exe';

async function prepare(root, signal, report) {
  if (process.env.PRISM_CLOUDFLARED) return process.env.PRISM_CLOUDFLARED;
  if (process.platform !== 'win32' || process.arch !== 'x64') return 'cloudflared';
  const target = path.join(root, 'tools', 'cloudflared.exe');
  const valid = async filename => (await fs.stat(filename)).size === SIZE && await hashFile(filename, signal) === CHECKSUM;
  if (existsSync(target) && await valid(target)) return target;
  await fs.mkdir(path.dirname(target), { recursive: true });
  const partial = target + '.download';
  let received = 0;
  const progress = new Transform({
    transform(chunk, _, callback) {
      received += chunk.length;
      if (received > SIZE) return callback(new Error('Неверный размер cloudflared'));
      report(`Загрузка сетевого компонента: ${Math.round(100 * received / SIZE)}%`);
      callback(null, chunk);
    }
  });
  try {
    const response = await fetch(RELEASE, { signal: AbortSignal.any([signal, AbortSignal.timeout(180_000)]) });
    if (!response.ok) throw new Error('Не удалось скачать cloudflared: ' + response.status);
    await pipeline(response.body, progress, createWriteStream(partial, { mode: 0o700 }), { signal });
    if (!await valid(partial)) throw new Error('Контрольная сумма cloudflared не совпадает');
    await fs.rename(partial, target);
    return target;
  } finally {
    await fs.rm(partial, { force: true });
  }
}

async function openTunnel({ root, url, check, signal, report, ended }) {
  const executable = await prepare(root, signal, report);
  signal.throwIfAborted();
  const launch = new AbortController();
  const startup = AbortSignal.any([signal, launch.signal, AbortSignal.timeout(85_000)]);
  const child = spawn(executable, ['tunnel', '--no-autoupdate', '--protocol', 'http2', '--url', url],
    { windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] });

  let output = '';
  let ready = false;
  let failure;
  const stop = () => { child.kill(); };
  signal.addEventListener('abort', stop, { once: true });
  const read = chunk => { output = (output + chunk.toString()).slice(-20000); };
  child.stdout.on('data', read);
  child.stderr.on('data', read);
  child.on('error', error => {
    failure = error;
    launch.abort();
  });
  child.on('close', () => {
    signal.removeEventListener('abort', stop);
    launch.abort();
    void fs.writeFile(path.join(root, 'tunnel.log'), output).catch(() => {});
    if (ready && !signal.aborted) ended();
  });

  report('Создаётся интернет-адрес…');
  try {
    while (!startup.aborted) {
      const match = /https:\/\/[a-z0-9-]+\.trycloudflare\.com\b/.exec(output);
      if (match) {
        try {
          await check(match[0], startup);
          startup.throwIfAborted();
          ready = true;
          return match[0];
        } catch {
          // The public URL may take a few seconds to accept requests.
        }
      }
      await delay(1200, undefined, { signal: startup });
    }
    startup.throwIfAborted();
  } catch {
    stop();
    if (failure?.code === 'ENOENT') throw new Error('Установите cloudflared или укажите путь в PRISM_CLOUDFLARED');
    if (signal.aborted) throw new Error('Запуск интернет-канала отменён');
    throw new Error('Интернет-канал не запустился. Проверьте tunnel.log и доступ к Cloudflare.');
  }
}

module.exports = { openTunnel };
