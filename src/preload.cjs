'use strict';

const { contextBridge, ipcRenderer } = require('electron');

const methods = [
  'preferredFolder', 'saveFolder', 'chooseFolder', 'host', 'join', 'disconnect',
  'internet', 'copyInvite', 'poll', 'createChat', 'send', 'storage', 'openFolder',
  'clean', 'upload', 'download', 'cancelTransfer'
];
contextBridge.exposeInMainWorld('prism', {
  ...Object.fromEntries(methods.map(name => [name, (...args) => ipcRenderer.invoke('prism:' + name, ...args)])),
  onStatus: callback => { ipcRenderer.on('prism:status', (_, message) => callback(message)); },
  onInternetEnded: callback => { ipcRenderer.on('prism:internet-ended', () => callback()); }
});
