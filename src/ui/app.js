'use strict';

const api = window.prism;
const byId = id => document.getElementById(id);
const status = text => { byId('progress').textContent = text; };
const show = id => byId(id).showModal();
const formatTime = new Intl.DateTimeFormat('ru', { hour: '2-digit', minute: '2-digit' });
let connection, polling, internet, startingInternet, transferring, sending, pendingMessage;
let chat = 'general';
let cursor = 0;
let revision = null;
let selection = 0;
let chatList = '';

function showError(error) {
  const message = (error.message || String(error)).replace(/^Error invoking remote method '[^']+': (?:\w+Error: |Error: )?/, '');
  const modal = document.querySelector('dialog[open]');
  if (modal) {
    let alert = modal.querySelector('.dialog-error');
    if (!alert) {
      alert = document.createElement('p');
      alert.className = 'dialog-error';
      alert.setAttribute('role', 'alert');
      modal.append(alert);
    }
    alert.textContent = message;
  } else {
    status(message);
  }
}

function listen(id, type, handler) {
  byId(id).addEventListener(type, event => {
    if (type === 'submit') event.preventDefault();
    Promise.resolve().then(() => handler(event)).catch(showError);
  });
}
const click = (id, handler) => listen(id, 'click', handler);
const submit = (id, handler) => listen(id, 'submit', async event => {
  const button = event.target.querySelector('button:not([type])');
  if (button?.disabled) return;
  if (button) button.disabled = true;
  try {
    await handler();
  } finally {
    if (button) button.disabled = false;
  }
});

function resetChat(id) {
  chat = id;
  cursor = 0;
  revision = null;
  selection++;
  chatList = '';
  byId('messages').replaceChildren();
}

function controls() {
  const connected = Boolean(connection);
  byId('welcome').hidden = connected;
  byId('chat').hidden = !connected;
  byId('new-chat').hidden = !connection?.user.is_host;
  byId('new-chat').disabled = transferring || sending;
  byId('internet').hidden = !connection?.hostsHere;
  byId('internet').disabled = Boolean(internet || startingInternet);
  byId('internet').textContent = internet ? 'Интернет включён' : 'Включить интернет';
  byId('invite').hidden = !connection?.hostsHere;
  byId('invite').disabled = !internet;
  byId('leave').disabled = transferring || sending || startingInternet;
  byId('cancel-transfer').hidden = !transferring;
  byId('file').disabled = transferring;
  byId('text').readOnly = sending;
  byId('send').disabled = sending;
  byId('settings').disabled = transferring;
  for (const button of byId('chats').querySelectorAll('button')) button.disabled = transferring || sending;
}

function ready(result) {
  connection = result;
  resetChat('general');
  byId('chat-title').textContent = 'Общий чат';
  byId('connection').textContent = result.user.name + ' · ' + result.url;
  controls();
  void poll();
}

function renderChats(chats, selected) {
  const key = JSON.stringify([chats.map(({ id, title }) => [id, title]), selected.id]);
  if (key === chatList) return;
  chatList = key;
  byId('chat-title').textContent = selected.title;
  const nav = byId('chats');
  nav.replaceChildren();
  for (const item of chats) {
    const button = document.createElement('button');
    button.textContent = item.title;
    button.setAttribute('aria-current', String(item.id === selected.id));
    button.addEventListener('click', () => {
      if (chat === item.id) return;
      resetChat(item.id);
      byId('chat-title').textContent = item.title;
      void poll();
    });
    nav.append(button);
  }
  controls();
}

function append(rows) {
  const list = byId('messages');
  for (const row of rows) {
    const item = document.createElement('li');
    item.classList.toggle('own', row.sender === connection.user.id);
    const meta = document.createElement('small');
    meta.textContent = row.sender_name + ' · ' + formatTime.format(new Date(row.created * 1000));
    item.append(meta);
    if (row.file) {
      const button = document.createElement('button');
      button.textContent = 'Скачать ' + row.file.name + ' · ' + humanSize(row.file.size);
      button.addEventListener('click', () => void transfer(() => api.download(row.file)).catch(showError));
      item.append(button);
    } else {
      item.append(document.createTextNode(row.text));
    }
    list.append(item);
    cursor = Math.max(cursor, row.id);
  }
  if (rows.length) list.scrollTop = list.scrollHeight;
}

async function poll() {
  if (!connection || polling) return;
  const active = connection;
  const generation = selection;
  polling = true;
  let more = false;
  try {
    const data = await api.poll(chat, cursor, revision);
    if (connection !== active || generation !== selection) return;
    if (data.reset) {
      resetChat(data.current.id);
    }
    revision = data.current.revision;
    renderChats(data.chats, data.current);
    append(data.rows);
    more = data.rows.length === 200;
    byId('connection').textContent = active.user.name + ' · ' + active.url
      + (data.pending ? ' · ожидается удаление отменённой отправки' : '');
  } catch {
    if (connection === active) byId('connection').textContent = 'Нет связи с сервером. Повторяем подключение…';
  } finally {
    polling = false;
    if (more || (connection && (active !== connection || (generation !== selection && revision === null)))) void poll();
  }
}

async function transfer(task) {
  if (transferring) return;
  transferring = true;
  byId('cancel-transfer').disabled = false;
  controls();
  try {
    status(await task());
  } finally {
    transferring = false;
    controls();
    void poll();
  }
}

function humanSize(bytes) {
  const units = ['Б', 'КиБ', 'МиБ', 'ГиБ', 'ТиБ'];
  let unit = 0;
  while (bytes >= 1024 && unit < units.length - 1) {
    bytes /= 1024;
    unit++;
  }
  return bytes.toLocaleString('ru', { maximumFractionDigits: 1 }) + ' ' + units[unit];
}

async function refreshStorage() {
  const data = await api.storage();
  const area = byId('storage');
  area.replaceChildren();
  byId('current-folder').textContent = data ? 'Текущая папка: ' + data.folder : '';
  byId('open-folder').hidden = !connection?.hostsHere;
  if (!data) return;
  const items = [{ title: 'Все чаты', id: null }, ...data.chats];
  for (const item of items) {
    const card = document.createElement('article');
    const title = document.createElement('strong');
    title.textContent = item.title;
    card.append(title);
    if (item.id !== null) {
      const sizes = document.createElement('p');
      sizes.textContent = `Сообщений: ${item.messages} · Вложения: ${humanSize(item.file_bytes)} · Кэш: ${humanSize(item.cache_bytes)}`;
      card.append(sizes);
    }
    const actions = document.createElement('div');
    actions.className = 'row';
    for (const [mode, name] of [['cache', 'Очистить кэш'], ['files', 'Удалить вложения'], ['all', 'Очистить полностью']]) {
      const button = document.createElement('button');
      button.textContent = name;
      button.addEventListener('click', async () => {
        for (const action of area.querySelectorAll('button')) action.disabled = true;
        try {
          if (await api.clean(mode, item.id)) {
            await refreshStorage();
            void poll();
          }
        } catch (error) {
          showError(error);
        } finally {
          for (const action of area.querySelectorAll('button')) action.disabled = false;
        }
      });
      actions.append(button);
    }
    card.append(actions);
    area.append(card);
  }
}

for (const dialog of document.querySelectorAll('dialog')) {
  dialog.addEventListener('close', () => dialog.querySelector('.dialog-error')?.remove());
}
for (const button of document.querySelectorAll('[data-close]')) {
  button.addEventListener('click', () => byId(button.dataset.close).close());
}
for (const prefix of ['host', 'settings']) {
  click(prefix + '-browse', async () => {
    const field = byId(prefix + '-folder');
    const folder = await api.chooseFolder(field.value);
    if (folder) field.value = folder;
  });
}

click('host-open', async () => {
  byId('host-folder').value = await api.preferredFolder();
  show('host-dialog');
});
submit('host-form', async () => {
  const result = await api.host(byId('host-folder').value, byId('host-port').valueAsNumber);
  byId('host-dialog').close();
  ready(result);
  status('Сервер запущен. Включите интернет, чтобы пригласить друга.');
});

click('join-open', () => show('join-dialog'));
byId('join-dialog').addEventListener('close', () => { byId('code').value = ''; });
submit('join-form', async () => {
  const result = await api.join(byId('code').value);
  byId('join-dialog').close();
  ready(result);
  status('');
});

click('internet', async () => {
  startingInternet = true;
  controls();
  try {
    internet = await api.internet();
    status('Интернет включён. Скопируйте приглашение для друга.');
  } finally {
    startingInternet = false;
    controls();
  }
});
api.onInternetEnded(() => {
  internet = null;
  controls();
  status('Интернет-канал закрыт. Включите его заново и отправьте новое приглашение.');
});
click('invite', async () => {
  await api.copyInvite();
  status('Приглашение скопировано.');
});
click('leave', async () => {
  await api.disconnect();
  connection = null;
  internet = null;
  pendingMessage = null;
  byId('text').value = '';
  byId('connection').textContent = 'Отключено';
  status('');
  controls();
});

click('new-chat', () => show('new-dialog'));
submit('new-form', async () => {
  const item = await api.createChat(byId('title').value);
  byId('title').value = '';
  byId('new-dialog').close();
  resetChat(item.id);
  void poll();
});

listen('composer', 'submit', async () => {
  const text = byId('text').value;
  if (!text.trim() || sending) return;
  sending = true;
  controls();
  if (!pendingMessage || pendingMessage.text !== text || pendingMessage.chat !== chat) {
    pendingMessage = { text, chat, id: crypto.randomUUID() };
  }
  try {
    await api.send(text, chat, pendingMessage.id);
    pendingMessage = null;
    byId('text').value = '';
    status('');
    void poll();
  } finally {
    sending = false;
    controls();
  }
});
click('file', () => transfer(() => api.upload(chat)));
click('cancel-transfer', async () => {
  byId('cancel-transfer').disabled = true;
  status('Отменяем передачу…');
  await api.cancelTransfer();
});

click('settings', async () => {
  byId('settings-folder').value = await api.preferredFolder();
  show('settings-dialog');
  await refreshStorage();
});
click('save-folder', async () => {
  byId('settings-folder').value = await api.saveFolder(byId('settings-folder').value);
  byId('settings-dialog').querySelector('.dialog-error')?.remove();
});
click('open-folder', () => api.openFolder());
api.onStatus(status);

setInterval(poll, 900);
controls();
