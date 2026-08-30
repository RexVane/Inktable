const assert = require('node:assert/strict');
const test = require('node:test');

const {
  credentialScope,
  normalizeEndpoint,
  prepareSlotConfig,
} = require('../electron/model-config');

test('a saved key is reused only inside the same provider and endpoint origin', () => {
  const previous = {
    provider: 'openai', endpoint: 'https://api.example.com/v1',
    api_key: 'old-secret', model: 'old-model',
  };
  const sameOrigin = prepareSlotConfig({
    provider: 'openai', endpoint: 'https://api.example.com/v2', model: 'new-model',
  }, previous, 'qa');
  assert.equal(sameOrigin.config.api_key, 'old-secret');
  assert.equal(credentialScope('openai', previous.endpoint),
    credentialScope('openai', sameOrigin.config.endpoint));

  assert.throws(() => prepareSlotConfig({
    provider: 'openai', endpoint: 'https://attacker.example/v1', model: 'new-model',
  }, previous, 'qa'), /重新输入 API 密钥/);
  const providerChanged = prepareSlotConfig({
    provider: 'ollama', endpoint: 'https://api.example.com/v1', model: 'new-model',
  }, previous, 'library');
  assert.equal(providerChanged.config.api_key, '');
});

test('remote plaintext, credentialed, query and fragment endpoints are rejected', () => {
  assert.equal(normalizeEndpoint('http://127.0.0.1:11434/'), 'http://127.0.0.1:11434');
  assert.throws(() => normalizeEndpoint('http://api.example.com/v1'), /HTTPS/);
  assert.throws(() => normalizeEndpoint('https://u:p@example.com/v1'), /用户名/);
  assert.throws(() => normalizeEndpoint('https://api.example.com/v1?key=x'), /查询参数/);
  assert.throws(() => normalizeEndpoint('file:///etc/passwd'), /http/);
});

test('embedding remains local Ollama and an explicit new key can change origin', () => {
  assert.throws(() => prepareSlotConfig({
    provider: 'openai', endpoint: 'https://api.example.com/v1',
    api_key: 'x', model: 'embed',
  }, null, 'embedding'), /Ollama/);
  const changed = prepareSlotConfig({
    provider: 'openai', endpoint: 'https://new.example/v1',
    api_key: 'new-secret', model: 'answer',
  }, {
    provider: 'openai', endpoint: 'https://old.example/v1',
    api_key: 'old-secret', model: 'answer',
  }, 'qa');
  assert.equal(changed.config.api_key, 'new-secret');
});
