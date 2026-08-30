'use strict';

const LOOPBACK_HOSTS = new Set(['localhost', '127.0.0.1', '::1']);
const SLOTS = new Set(['qa', 'library', 'embedding']);
const QA_PROVIDERS = new Set(['openai', 'ollama', 'anthropic', 'responses']);
const LIBRARY_PROVIDERS = new Set(['openai', 'ollama']);
const CLOUD_PROVIDERS = new Set(['openai', 'anthropic', 'responses']);

function normalizeEndpoint(raw) {
  const value = String(raw || '').trim().replace(/\/+$/, '');
  if (!value) throw new Error('接口地址不能为空');
  let parsed;
  try { parsed = new URL(value); } catch { throw new Error('接口地址无效'); }
  if (!['http:', 'https:'].includes(parsed.protocol) || !parsed.hostname) {
    throw new Error('接口地址必须是 http:// 或 https:// URL');
  }
  if (parsed.username || parsed.password) throw new Error('接口地址不能包含用户名或密码');
  if (parsed.search || parsed.hash) throw new Error('接口地址不能包含查询参数或片段');
  const host = parsed.hostname.toLowerCase().replace(/\.$/, '');
  if (parsed.protocol === 'http:' && !LOOPBACK_HOSTS.has(host)) {
    throw new Error('远程模型接口必须使用 HTTPS；HTTP 只允许本机服务');
  }
  return value;
}

function providerFamily(provider) {
  return String(provider || '').trim().toLowerCase() === 'ollama' ? 'ollama' : 'cloud';
}

function credentialScope(provider, endpoint) {
  const parsed = new URL(normalizeEndpoint(endpoint));
  return `${providerFamily(provider)}|${parsed.origin.toLowerCase()}`;
}

function prepareSlotConfig(incoming, previous, requestedSlot) {
  if (!incoming || typeof incoming !== 'object') throw new TypeError('模型配置格式无效');
  const slot = SLOTS.has(requestedSlot) ? requestedSlot : 'qa';
  const prev = previous || {};
  const provider = String(incoming.provider || prev.provider ||
    (slot === 'qa' ? 'openai' : 'ollama')).trim().toLowerCase();
  if (slot === 'qa' && !QA_PROVIDERS.has(provider)) {
    throw new Error('未知模型服务类型');
  }
  if (slot === 'library' && !LIBRARY_PROVIDERS.has(provider)) {
    throw new Error('整理槽位目前只支持 OpenAI 兼容或本地 Ollama');
  }
  if (slot === 'embedding' && provider !== 'ollama') {
    throw new Error('向量模型目前只支持本地 Ollama');
  }
  const endpoint = normalizeEndpoint(incoming.endpoint || '');
  const model = String(incoming.model || '').trim();
  if (!model) throw new Error('必须填写模型名');
  const enteredKey = String(incoming.api_key || '').trim();
  let apiKey = enteredKey;
  if (!apiKey && prev.api_key && prev.provider && prev.endpoint) {
    let sameScope = false;
    try {
      sameScope = credentialScope(provider, endpoint) ===
        credentialScope(String(prev.provider), String(prev.endpoint));
    } catch {}
    if (sameScope) apiKey = prev.api_key;
  }
  if (CLOUD_PROVIDERS.has(provider) && !apiKey) {
    throw new Error('更换服务类型或地址后必须重新输入 API 密钥');
  }
  return { slot, config: { provider, endpoint, api_key: apiKey, model } };
}

module.exports = { credentialScope, normalizeEndpoint, prepareSlotConfig };
