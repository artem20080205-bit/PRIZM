'use strict';

const { createReadStream } = require('node:fs');
const { createHash } = require('node:crypto');

const UUID = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i;
const SHA256 = /^[a-f0-9]{64}$/;

class HttpError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

async function hashFile(filename, signal) {
  const hash = createHash('sha256');
  for await (const chunk of createReadStream(filename, { signal })) hash.update(chunk);
  signal?.throwIfAborted();
  return hash.digest('hex');
}

module.exports = { UUID, SHA256, HttpError, hashFile };
