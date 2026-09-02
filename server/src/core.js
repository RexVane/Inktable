'use strict';

const crypto = require('node:crypto');

class AppError extends Error {
  constructor(statusCode, code, message, details) {
    super(message);
    this.name = 'AppError';
    this.statusCode = statusCode;
    this.code = code;
    this.details = details;
  }
}

const now = () => new Date().toISOString();
const id = prefix => `${prefix}_${crypto.randomUUID().replaceAll('-', '')}`;
const hash = value => crypto.createHash('sha256').update(value).digest('hex');
const stableJson = value => {
  const normalize = input => {
    if (Array.isArray(input)) return input.map(normalize);
    if (input && typeof input === 'object') return Object.fromEntries(Object.keys(input).sort().map(key => [key, normalize(input[key])]));
    return input;
  };
  return JSON.stringify(normalize(value));
};
const parseJson = (value, fallback = null) => {
  if (value === null || value === undefined || value === '') return fallback;
  try { return JSON.parse(value); } catch { return fallback; }
};
const required = (value, name) => {
  if (value === undefined || value === null || String(value).trim() === '') {
    throw new AppError(400, 'VALIDATION_ERROR', `${name} 为必填项`, { field: name });
  }
  return typeof value === 'string' ? value.trim() : value;
};
const boundedInt = (value, fallback, min, max, name = 'value') => {
  const number = value === undefined ? fallback : Number(value);
  if (!Number.isInteger(number) || number < min || number > max) {
    throw new AppError(400, 'VALIDATION_ERROR', `${name} 必须是 ${min} 到 ${max} 之间的整数`, { field: name });
  }
  return number;
};
const page = query => ({
  limit: boundedInt(query?.limit, 50, 1, 200, 'limit'),
  offset: boundedInt(query?.offset, 0, 0, 1_000_000, 'offset')
});
const safeName = name => String(name || 'file').replace(/[\x00-\x1f<>:"/\\|?*]/g, '_').slice(0, 180);
const redact = value => String(value || '')
  .replace(/(api[_-]?key|password|secret|token|authorization)\s*[:=]\s*[^\s,;]+/gi, '$1=[REDACTED]')
  .replace(/(postgres(?:ql)?|mysql|mongodb):\/\/[^\s]+/gi, '[REDACTED_CONNECTION]');

function rowToObject(row) {
  if (!row) return row;
  const result = { ...row };
  for (const [key, value] of Object.entries(result)) {
    if (key.endsWith('_json')) result[key.slice(0, -5)] = parseJson(value, {});
    if (['deleted_at'].includes(key) && value === null) delete result[key];
  }
  return result;
}

module.exports = {
  AppError, now, id, hash, stableJson, parseJson, required, boundedInt, page, safeName, redact, rowToObject
};
