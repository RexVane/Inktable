const assert = require('node:assert/strict');
const test = require('node:test');

const {
  credentialScope,
  normalizeEndpoint,
  prepareSlotConfig,
} = require('../electron/model-config');

test('a saved key is reused only inside the same provider family and endpoint origin', () => {
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

  const protocolChanged = prepareSlotConfig({
    provider: 'responses', endpoint: 'https://api.example.com/v1', model: 'new-model',
  }, previous, 'qa');
  assert.equal(protocolChanged.config.api_key, 'old-secret');
  assert.equal(protocolChanged.config.provider, 'responses');

  assert.throws(() => prepareSlotConfig({
    provider: 'openai', endpoint: 'https://attacker.example/v1', model: 'new-model',
  }, previous, 'qa'), /重新输入 API 密钥/);
  const providerChanged = prepareSlotConfig({
    provider: 'ollama', endpoint: 'https://api.example.com/v1', model: 'new-model',
  }, previous, 'library');
  assert.equal(providerChanged.config.api_key, '');
});

test('qa accepts protocol providers that library and embedding reject', () => {
  const qaAnthropic = prepareSlotConfig({
    provider: 'anthropic', endpoint: 'https://api.anthropic.com/v1',
    api_key: 'sk-ant', model: 'claude-sonnet',
  }, null, 'qa');
  assert.equal(qaAnthropic.config.provider, 'anthropic');
  assert.throws(() => prepareSlotConfig({
    provider: 'anthropic', endpoint: 'https://api.anthropic.com/v1',
    api_key: 'sk-ant', model: 'claude-sonnet',
  }, null, 'library'), /整理槽位/);
  assert.throws(() => prepareSlotConfig({
    provider: 'responses', endpoint: 'https://api.example.com/v1',
    api_key: 'sk-x', model: 'embed',
  }, null, 'embedding'), /Ollama/);
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
