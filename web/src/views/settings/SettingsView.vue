<template>
  <div>
    <div class="page-head">
      <div>
        <h1>系统设置</h1>
        <p>个性化偏好、本地 AI 模型接口与底层持久化存储管理</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" style="background:var(--accent);color:#fff;height:36px;padding:0 18px;border-radius:6px;font-size:13.5px;" @click="appStore.showToast('设置已安全持久化！', 'ok')">保存设置</button>
      </div>
    </div>

    <div style="min-height:calc(100vh - 175px);display:grid;grid-template-columns:220px 1fr;gap:20px;">
      <!-- Sub-Nav -->
      <div class="card" style="margin:0;display:flex;flex-direction:column;height:100%;padding:10px;">
        <div v-for="tab in tabs" :key="tab.id" :class="['dataset-list-item', { active: activeTab === tab.id }]" style="padding:12px 16px;border-radius:6px;cursor:pointer;font-size:13.5px;font-weight:600;" :style="{ background: activeTab === tab.id ? 'var(--accent-soft)' : 'transparent', color: activeTab === tab.id ? 'var(--accent)' : 'var(--ink)' }" @click="activeTab = tab.id">
          {{ tab.label }}
        </div>
      </div>

      <!-- Main Config Pane -->
      <div class="card" style="margin:0;display:flex;flex-direction:column;height:100%;padding:24px 30px;">
        <!-- General -->
        <div v-if="activeTab === 'general'">
          <h2 style="font-size:16px;font-weight:700;color:var(--ink-strong);margin-bottom:18px;border-bottom:1px solid var(--line);padding-bottom:10px;">界面与主题偏好</h2>
          <div class="form-group" style="margin-bottom:20px;">
            <label class="muted" style="font-size:13px;margin-bottom:8px;display:block;">色彩主题模式</label>
            <div style="display:flex;gap:16px;font-size:13.5px;">
              <label style="cursor:pointer;"><input type="radio" name="theme" :checked="appStore.theme === '浅色'" @change="appStore.setTheme('浅色')"> 浅色 (Silver)</label>
              <label style="cursor:pointer;"><input type="radio" name="theme" :checked="appStore.theme === '深色'" @change="appStore.setTheme('深色')"> 深色 (Nebula)</label>
              <label style="cursor:pointer;"><input type="radio" name="theme" :checked="appStore.theme === '跟随系统'" @change="appStore.setTheme('跟随系统')"> 跟随系统</label>
            </div>
          </div>
        </div>

        <!-- Models -->
        <div v-else-if="activeTab === 'models'">
          <h2 style="font-size:16px;font-weight:700;color:var(--ink-strong);margin-bottom:18px;border-bottom:1px solid var(--line);padding-bottom:10px;">大模型接口与密钥</h2>
          <div class="form-group" style="margin-bottom:18px;">
            <label class="muted" style="font-size:13px;margin-bottom:6px;display:block;">模型服务提供商</label>
            <input class="input" value="OpenAI Compatible (本地 Ollama / vLLM)" style="height:36px;font-size:13px;padding:0 12px;border-radius:6px;background:var(--card-bg);border:1px solid var(--line);color:var(--ink);width:100%;">
          </div>
          <div class="form-group" style="margin-bottom:18px;">
            <label class="muted" style="font-size:13px;margin-bottom:6px;display:block;">Base URL</label>
            <input class="input" value="http://127.0.0.1:11434/v1" style="height:36px;font-size:13px;padding:0 12px;border-radius:6px;background:var(--card-bg);border:1px solid var(--line);color:var(--ink);width:100%;">
          </div>
        </div>

        <!-- Storage -->
        <div v-else-if="activeTab === 'storage'">
          <h2 style="font-size:16px;font-weight:700;color:var(--ink-strong);margin-bottom:18px;border-bottom:1px solid var(--line);padding-bottom:10px;">本地数据与缓存路径</h2>
          <div class="form-group" style="margin-bottom:18px;">
            <label class="muted" style="font-size:13px;margin-bottom:6px;display:block;">SQLite 数据库文件</label>
            <code class="mono" style="background:var(--inset);padding:8px 12px;border-radius:6px;display:block;">D:/AIApp/Ordo/data/ordo.db (38.4 MB)</code>
          </div>
          <button class="btn" style="border:1px solid var(--line);background:var(--card-bg);padding:6px 14px;border-radius:6px;font-size:13px;" @click="appStore.showToast('已清理临时解析缓存')">🧹 清理临时缓存</button>
        </div>

        <!-- Version -->
        <div v-else-if="activeTab === 'version'">
          <h2 style="font-size:16px;font-weight:700;color:var(--ink-strong);margin-bottom:18px;border-bottom:1px solid var(--line);padding-bottom:10px;">产品与运行环境</h2>
          <div style="font-size:13.5px;line-height:1.8;">
            <div>产品名称: <b>Ordo 本地知识引擎工作台</b></div>
            <div>系统版本: <span class="mono" style="color:var(--accent);font-weight:700;">v1.8.0-enterprise</span></div>
            <div>前端架构: <span class="badge ok">Vue 3.4 + Vite 5 + Pinia 2</span></div>
            <div>后端服务: <span class="badge ok">Node.js 24 + Fastify + SQLite</span></div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref } from 'vue';
import { useAppStore } from '@/stores/app';

const appStore = useAppStore();
const activeTab = ref('general');

const tabs = [
  { id: 'general', label: '通用偏好' },
  { id: 'models', label: 'AI 模型' },
  { id: 'storage', label: '存储管理' },
  { id: 'version', label: '版本信息' }
];
</script>
