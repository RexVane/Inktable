/**
 * Ordo Enterprise Web Widget Runtime (v1.0.0)
 * Lightweight, zero-dependency, self-contained AI assistant overlay for enterprise websites.
 * Embed via: <script src="http://127.0.0.1:8790/widget.js" data-assistant-id="YOUR_ASSISTANT_ID" defer></script>
 */
(function () {
  'use strict';

  // 1. Identify script configuration
  const currentScript = document.currentScript || document.querySelector('script[data-assistant-id]');
  const assistantId = currentScript ? currentScript.getAttribute('data-assistant-id') : null;
  const scriptSrc = currentScript ? currentScript.src : '';
  let apiBase = 'http://127.0.0.1:8790';
  if (scriptSrc) {
    try {
      const url = new URL(scriptSrc);
      apiBase = url.origin;
    } catch (e) {}
  }

  // Prevent double injection
  if (window.__ordoWidgetLoaded) return;
  window.__ordoWidgetLoaded = true;

  // 2. Build Container & Isolated Styles
  const hostDiv = document.createElement('div');
  hostDiv.id = 'ordo-widget-host';
  document.body.appendChild(hostDiv);

  const shadow = hostDiv.attachShadow ? hostDiv.attachShadow({ mode: 'open' }) : hostDiv;

  const style = document.createElement('style');
  style.textContent = `
    * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
    .ordo-launcher {
      position: fixed;
      bottom: 24px;
      right: 24px;
      width: 58px;
      height: 58px;
      border-radius: 50%;
      background: linear-gradient(135deg, #0f8b4c 0%, #056133 100%);
      box-shadow: 0 4px 18px rgba(15, 139, 76, 0.45);
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: center;
      color: #ffffff;
      font-size: 26px;
      z-index: 999999;
      transition: all 0.25s cubic-bezier(0.16, 1, 0.3, 1);
      user-select: none;
    }
    .ordo-launcher:hover {
      transform: scale(1.08) translateY(-2px);
      box-shadow: 0 8px 24px rgba(15, 139, 76, 0.55);
    }
    .ordo-chat-window {
      position: fixed;
      bottom: 96px;
      right: 24px;
      width: 380px;
      height: 560px;
      max-width: calc(100vw - 32px);
      max-height: calc(100vh - 120px);
      background: #0d1520;
      border: 1px solid rgba(255, 255, 255, 0.12);
      border-radius: 16px;
      box-shadow: 0 12px 36px rgba(0, 0, 0, 0.45);
      z-index: 999998;
      display: flex;
      flex-direction: column;
      overflow: hidden;
      opacity: 0;
      pointer-events: none;
      transform: translateY(16px) scale(0.96);
      transition: all 0.25s cubic-bezier(0.16, 1, 0.3, 1);
      color: #e2e8f0;
    }
    .ordo-chat-window.open {
      opacity: 1;
      pointer-events: auto;
      transform: translateY(0) scale(1);
    }
    .ordo-header {
      background: #152232;
      border-bottom: 1px solid rgba(255, 255, 255, 0.08);
      padding: 14px 18px;
      display: flex;
      align-items: center;
      justify-content: space-between;
    }
    .ordo-title-area {
      display: flex;
      align-items: center;
      gap: 10px;
    }
    .ordo-avatar {
      width: 34px;
      height: 34px;
      border-radius: 8px;
      background: rgba(15, 139, 76, 0.2);
      border: 1px solid #0f8b4c;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 18px;
    }
    .ordo-title {
      font-size: 14.5px;
      font-weight: 600;
      color: #f8fafc;
    }
    .ordo-status {
      font-size: 11px;
      color: #0f8b4c;
      display: flex;
      align-items: center;
      gap: 4px;
      margin-top: 2px;
    }
    .ordo-status-dot {
      width: 6px;
      height: 6px;
      border-radius: 50%;
      background: #0f8b4c;
      box-shadow: 0 0 6px #0f8b4c;
    }
    .ordo-close-btn {
      background: transparent;
      border: none;
      color: #94a3b8;
      font-size: 20px;
      cursor: pointer;
      width: 28px;
      height: 28px;
      display: flex;
      align-items: center;
      justify-content: center;
      border-radius: 6px;
      transition: all 0.15s;
    }
    .ordo-close-btn:hover {
      background: rgba(255, 255, 255, 0.08);
      color: #f8fafc;
    }
    .ordo-messages {
      flex: 1;
      overflow-y: auto;
      padding: 16px;
      display: flex;
      flex-direction: column;
      gap: 12px;
    }
    .ordo-msg {
      max-width: 85%;
      padding: 10px 14px;
      border-radius: 12px;
      font-size: 13.5px;
      line-height: 1.5;
      word-break: break-word;
    }
    .ordo-msg.user {
      align-self: flex-end;
      background: #0f8b4c;
      color: #ffffff;
      border-bottom-right-radius: 4px;
    }
    .ordo-msg.assistant {
      align-self: flex-start;
      background: #1e293b;
      color: #f1f5f9;
      border: 1px solid rgba(255, 255, 255, 0.08);
      border-bottom-left-radius: 4px;
    }
    .ordo-citation {
      display: inline-block;
      margin-top: 6px;
      padding: 2px 8px;
      background: rgba(15, 139, 76, 0.15);
      border: 1px solid rgba(15, 139, 76, 0.4);
      border-radius: 4px;
      font-size: 11.5px;
      color: #34d399;
      cursor: pointer;
    }
    .ordo-footer {
      border-top: 1px solid rgba(255, 255, 255, 0.08);
      background: #152232;
      padding: 12px;
    }
    .ordo-input-row {
      display: flex;
      gap: 8px;
    }
    .ordo-input {
      flex: 1;
      background: #090e17;
      border: 1px solid rgba(255, 255, 255, 0.15);
      border-radius: 8px;
      padding: 8px 12px;
      color: #f8fafc;
      font-size: 13px;
      outline: none;
      transition: border-color 0.2s;
    }
    .ordo-input:focus {
      border-color: #0f8b4c;
    }
    .ordo-send-btn {
      background: #0f8b4c;
      border: none;
      color: #ffffff;
      border-radius: 8px;
      padding: 0 14px;
      font-size: 14px;
      font-weight: 600;
      cursor: pointer;
      transition: background 0.15s;
    }
    .ordo-send-btn:hover {
      background: #056133;
    }
    .ordo-branding {
      font-size: 10.5px;
      color: #64748b;
      text-align: center;
      margin-top: 8px;
    }
  `;
  shadow.appendChild(style);

  // 3. Render HTML
  const wrapper = document.createElement('div');
  wrapper.innerHTML = `
    <div class="ordo-launcher" id="ordoLauncher" title="智能知识客服">💬</div>
    <div class="ordo-chat-window" id="ordoChatWindow">
      <div class="ordo-header">
        <div class="ordo-title-area">
          <div class="ordo-avatar">🤖</div>
          <div>
            <div class="ordo-title">企业智能知识助手</div>
            <div class="ordo-status"><span class="ordo-status-dot"></span>在线 · 基于权威知识库</div>
          </div>
        </div>
        <button class="ordo-close-btn" id="ordoCloseBtn">✕</button>
      </div>
      <div class="ordo-messages" id="ordoMessages">
        <div class="ordo-msg assistant">
          您好！我是企业智能知识助手。您可以向我咨询关于产品手册、业务流程及常见问题的任何知识。
        </div>
      </div>
      <div class="ordo-footer">
        <div class="ordo-input-row">
          <input type="text" class="ordo-input" id="ordoInput" placeholder="输入您的问题..." />
          <button class="ordo-send-btn" id="ordoSendBtn">发送</button>
        </div>
        <div class="ordo-branding">Powered by Ordo Local-First Knowledge Engine</div>
      </div>
    </div>
  `;
  shadow.appendChild(wrapper);

  // 4. Interactive State & Networking
  const launcher = shadow.getElementById('ordoLauncher');
  const chatWindow = shadow.getElementById('ordoChatWindow');
  const closeBtn = shadow.getElementById('ordoCloseBtn');
  const messagesContainer = shadow.getElementById('ordoMessages');
  const inputEl = shadow.getElementById('ordoInput');
  const sendBtn = shadow.getElementById('ordoSendBtn');

  let isOpen = false;
  let visitorSessionId = null;

  function toggleChat() {
    isOpen = !isOpen;
    if (isOpen) {
      chatWindow.classList.add('open');
      launcher.textContent = '✕';
      inputEl.focus();
    } else {
      chatWindow.classList.remove('open');
      launcher.textContent = '💬';
    }
  }

  launcher.addEventListener('click', toggleChat);
  closeBtn.addEventListener('click', toggleChat);

  function appendMessage(role, text, citations = []) {
    const msg = document.createElement('div');
    msg.className = `ordo-msg ${role}`;
    msg.textContent = text;
    if (citations && citations.length > 0) {
      const citeBox = document.createElement('div');
      citations.forEach((c, idx) => {
        const span = document.createElement('span');
        span.className = 'ordo-citation';
        span.textContent = `[${idx + 1}] ${c.title || '证据来源'}`;
        citeBox.appendChild(span);
      });
      msg.appendChild(citeBox);
    }
    messagesContainer.appendChild(msg);
    messagesContainer.scrollTop = messagesContainer.scrollHeight;
  }

  async function handleSend() {
    const text = inputEl.value.trim();
    if (!text) return;
    inputEl.value = '';
    appendMessage('user', text);

    // Typing indicator
    const typingMsg = document.createElement('div');
    typingMsg.className = 'ordo-msg assistant';
    typingMsg.textContent = '正在检索知识库并生成回答...';
    messagesContainer.appendChild(typingMsg);
    messagesContainer.scrollTop = messagesContainer.scrollHeight;

    try {
      // Use public widget session endpoints
      const res = await fetch(`${apiBase}/api/v1/public/widget/sessions`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ assistantId, origin: window.location.origin })
      });
      const data = await res.json();
      const sessionId = data.data?.id || visitorSessionId || 'default_session';
      visitorSessionId = sessionId;

      const askRes = await fetch(`${apiBase}/api/v1/public/widget/sessions/${sessionId}/messages`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ query: text, origin: window.location.origin })
      });
      const askData = await askRes.json();
      typingMsg.remove();
      if (askData.data?.answer) {
        appendMessage('assistant', askData.data.answer, askData.data.citations || []);
      } else {
        appendMessage('assistant', askData.data?.content || askData.error?.message || '无法获取知识库回答');
      }
    } catch (e) {
      typingMsg.remove();
      appendMessage('assistant', '网络连接异常，请确保 Ordo 服务端正在运行。');
    }
  }

  sendBtn.addEventListener('click', handleSend);
  inputEl.addEventListener('keydown', function (e) {
    if (e.key === 'Enter') handleSend();
  });
})();
