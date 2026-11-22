'use strict';

const { app, BrowserWindow, Menu, clipboard, dialog, ipcMain, shell } = require('electron');
const { randomUUID } = require('node:crypto');
const fs = require('node:fs/promises');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const { createPrismServer } = require('./server.cjs');
const { Client, TransferState, invite, parseInvite } = require('./client.cjs');
const { openTunnel } = require('./tunnel.cjs');

const root = path.resolve(__dirname, '..');
const page = path.join(__dirname, 'ui', 'index.html');
const preferences = path.join(root, 'preferences.json');
let window, state, client, user, host, tunnel, busy, activeTransfer, transferJob;
let quitting = false;

function status(message) {
  if (window && !window.isDestroyed()) window.webContents.send('prism:status', message);
}

function connected() {
  if (!client) throw new Error('Сначала подключитесь к чату');
  return client;
}

async function preferredFolder() {
  try {
    const { server_folder: folder } = JSON.parse(await fs.readFile(preferences, 'utf8'));
    if (typeof folder === 'string' && folder.trim()) return folder;
  } catch (error) {
    if (error.code !== 'ENOENT' && !(error instanceof SyntaxError)) throw error;
  }
  return path.join(root, 'server_data_v02');
}

async function saveFolder(folder) {
  if (typeof folder !== 'string' || !folder.trim()) throw new Error('Укажите папку данных');
  const absolute = path.resolve(folder.trim());
  const temp = preferences + '.tmp';
  await fs.writeFile(temp, JSON.stringify({ server_folder: absolute }, null, 2), { mode: 0o600 });
  await fs.rename(temp, preferences);
  return absolute;
}

async function connect(connection) {
  const api = new Client(connection, state);
  const me = await api.call('GET', '/api/me', { timeout: 8000 });
  if (me.protocol !== 3) throw new Error('Нужен сервер PRISM с протоколом версии 3');
  client = api;
  user = me;
  return { user, url: api.url, hostsHere: Boolean(host) };
}

async function disconnect() {
  const server = host;
  host = null;
  client = null;
  user = null;
  tunnel?.controller.abort();
  tunnel = null;
  if (server) await server.close();
}

function handle(name, action) {
  ipcMain.handle('prism:' + name, async (event, ...args) => {
    if (event.sender !== window?.webContents || event.senderFrame?.url !== pathToFileURL(page).href) {
      throw new Error('Недоступное действие');
    }
    return action(...args);
  });
}

async function exclusive(action) {
  if (busy || quitting) throw new Error('Дождитесь завершения текущего действия');
  busy = Promise.resolve().then(action);
  try {
    return await busy;
  } finally {
    busy = null;
  }
}

async function transfer(action) {
  if (transferJob || quitting) throw new Error('Дождитесь завершения текущей передачи');
  const api = connected();
  const controller = new AbortController();
  activeTransfer = controller;
  transferJob = Promise.resolve().then(() => action(api, controller.signal));
  try {
    return await transferJob;
  } finally {
    activeTransfer = null;
    transferJob = null;
  }
}

function register() {
  handle('preferredFolder', preferredFolder);
  handle('saveFolder', folder => exclusive(() => saveFolder(folder)));
  handle('chooseFolder', async folder => {
    const result = await dialog.showOpenDialog(window, { defaultPath: folder, properties: ['openDirectory', 'createDirectory'] });
    return result.filePaths[0] ?? null;
  });
  handle('host', (folder, port) => exclusive(async () => {
    if (client) throw new Error('Сначала отключитесь от текущего сервера');
    if (!Number.isInteger(port) || port < 1024 || port > 65535) throw new Error('Укажите порт от 1024 до 65535');
    if (typeof folder !== 'string' || !folder.trim()) throw new Error('Укажите папку данных');
    const instance = createPrismServer(folder.trim());
    try {
      const url = await instance.listen(port);
      host = instance;
      const result = await connect({ url, token: host.users[0].token });
      await saveFolder(host.root);
      return result;
    } catch (error) {
      if (host === instance) await disconnect();
      else await instance.close();
      throw error;
    }
  }));
  handle('join', code => exclusive(() => {
    if (client) throw new Error('Сначала отключитесь от текущего сервера');
    return connect(parseInvite(code));
  }));
  handle('disconnect', () => exclusive(async () => {
    if (transferJob) throw new Error('Дождитесь завершения передачи');
    await disconnect();
  }));
  handle('internet', async () => {
    if (!host || !client) throw new Error('Сначала запустите свой сервер');
    if (tunnel) throw new Error('Интернет-канал уже включается или работает');
    const instance = host;
    const controller = new AbortController();
    tunnel = { controller };
    try {
      const url = await openTunnel({
        root, url: client.url, signal: controller.signal, report: status,
        check: (url, signal) => new Client({ url, token: instance.users[1].token }, state)
          .call('GET', '/api/me', { signal, timeout: 6000 }),
        ended: () => {
          if (tunnel?.controller !== controller) return;
          tunnel = null;
          if (window && !window.isDestroyed()) window.webContents.send('prism:internet-ended');
        }
      });
      controller.signal.throwIfAborted();
      tunnel.url = url;
      return url;
    } catch (error) {
      if (tunnel?.controller === controller) tunnel = null;
      throw error;
    }
  });
  handle('copyInvite', () => {
    if (!tunnel?.url || !host) throw new Error('Сначала включите интернет');
    clipboard.writeText(invite(tunnel.url, host.users[1].token));
  });
  handle('poll', async (chatId, cursor, revision) => {
    const api = connected();
    // A failed cancellation must not delay polling of messages.
    void api.flushCancellations().catch(() => {});
    const timeout = 7000;
    const chats = await api.call('GET', '/api/chats', { timeout });
    if (!chats.length) throw new Error('Сервер не вернул список чатов');
    const current = chats.find(chat => chat.id === chatId) ?? chats[0];
    const reset = current.id !== chatId || current.revision !== revision;
    const rows = await api.call('GET', '/api/messages', { query: { chat_id: current.id, after: reset ? 0 : cursor }, timeout });
    return { chats, current, reset, rows, pending: state.pending(api.profile).length };
  });
  handle('createChat', title => connected().call('POST', '/api/chats', { json: { title }, timeout: 7000 }));
  handle('send', (text, chatId, id) => connected().call('POST', '/api/messages', {
    query: { chat_id: chatId }, json: { text, client_id: id || randomUUID() }, timeout: 10000
  }));
  handle('storage', () => user?.is_host ? connected().call('GET', '/api/storage', { timeout: 7000 }) : null);
  handle('openFolder', async () => {
    if (!host) throw new Error('Папка доступна на компьютере хозяина');
    const error = await shell.openPath(host.root);
    if (error) throw new Error(error);
  });
  handle('clean', async (mode, chatId) => {
    if (!user?.is_host) throw new Error('Очистка доступна только хозяину');
    const labels = {
      cache: 'Удалить незавершённые отправки?',
      files: 'Удалить вложения и незавершённые отправки?',
      all: 'Удалить переписку, вложения и незавершённые отправки?'
    };
    if (!Object.hasOwn(labels, mode)) throw new Error('Некорректный режим очистки');
    const api = connected();
    const { response } = await dialog.showMessageBox(window, {
      type: 'warning', title: 'Очистка хранилища', message: labels[mode],
      detail: chatId ? 'Очистка затронет только выбранный чат.' : 'Очистка затронет все чаты.',
      buttons: ['Отмена', 'Удалить'], defaultId: 0, cancelId: 0
    });
    if (response !== 1 || client !== api) return null;
    return api.call('POST', '/api/storage/clear', { json: { mode, chat_id: chatId } });
  });
  handle('upload', chatId => transfer(async (api, signal) => {
    const result = await dialog.showOpenDialog(window, { properties: ['openFile'] });
    if (result.canceled) return '';
    return api.upload(result.filePaths[0], chatId, signal, status);
  }));
  handle('download', file => transfer(async (api, signal) => {
    const filename = String(file.name ?? 'Файл').split(/[\\/]/).at(-1).replace(/[<>:"|?*\x00-\x1f]/g, '_').replace(/[. ]+$/, '');
    const result = await dialog.showSaveDialog(window, { defaultPath: path.join(app.getPath('downloads'), filename || 'Файл') });
    if (result.canceled) return '';
    return api.download(file, result.filePath, signal, status);
  }));
  handle('cancelTransfer', () => activeTransfer?.abort());
}

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', () => {
    if (window?.isMinimized()) window.restore();
    window?.focus();
  });
  app.whenReady().then(() => {
    state = new TransferState(path.join(root, 'client_state.sqlite'));
    register();
    Menu.setApplicationMenu(null);
    window = new BrowserWindow({
      title: 'PRISM',
      width: 1000,
      height: 720,
      minWidth: 700,
      minHeight: 530,
      webPreferences: {
        preload: path.join(__dirname, 'preload.cjs'),
        sandbox: true,
        contextIsolation: true,
        nodeIntegration: false
      }
    });
    window.webContents.on('will-navigate', event => event.preventDefault());
    window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
    window.webContents.session.setPermissionRequestHandler((_, __, callback) => callback(false));
    return window.loadFile(page);
  }).catch(error => {
    dialog.showErrorBox('PRISM', error.message);
    app.quit();
  });

  app.on('window-all-closed', () => app.quit());
  app.on('before-quit', event => {
    if (quitting || !state) return;
    event.preventDefault();
    quitting = true;
    activeTransfer?.abort();
    void Promise.allSettled([busy, transferJob])
      .then(disconnect)
      .catch(error => console.error(error.message))
      .finally(() => {
        // A network poll can still be unwinding after its window closed.
        app.quit();
      });
  });
}
