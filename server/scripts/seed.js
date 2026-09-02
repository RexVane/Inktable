#!/usr/bin/env node
'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { createApp } = require('../src/app');

async function main() {
  const app = await createApp({ logger: false });
  const { db, knowledge, models, tasks, config } = app.services;
  let kb = db.one("SELECT * FROM knowledge_bases WHERE workspace_id=? AND name='Ordo 示例知识库' AND status!='deleted'", config.localWorkspaceId);
  if (!kb) kb = knowledge.createKnowledgeBase({ name: 'Ordo 示例知识库', description: '用于验证真实产品闭环的公开模拟数据' });
  const dataset = db.one("SELECT * FROM datasets WHERE knowledge_base_id=? AND status!='deleted' ORDER BY created_at LIMIT 1", kb.id);
  if (!db.one("SELECT 1 AS found FROM model_connections WHERE workspace_id=? AND provider='local-extractive'", config.localWorkspaceId)) {
    await models.create({ name: '本地严格证据模型', provider: 'local-extractive', purpose: 'generation', modelId: 'ordo-local-extractive-v1' });
  }
  const sample = path.resolve(__dirname, '..', 'fixtures', 'ordo-sample-knowledge.md');
  const content = fs.readFileSync(sample);
  const source = db.one("SELECT * FROM sources WHERE workspace_id=? AND dataset_id=? AND name='Ordo 示例产品知识' AND deleted_at IS NULL ORDER BY created_at LIMIT 1", config.localWorkspaceId, dataset.id)
    || knowledge.createSource(dataset.id, { type: 'synthetic', name: 'Ordo 示例产品知识', locationHint: 'server/fixtures/ordo-sample-knowledge.md' });
  const registered = await knowledge.registerUpload(dataset.id, source.id, path.basename(sample), content, 'text/markdown');
  if (registered.task) await tasks.wait(registered.task.id, config.localWorkspaceId, 30_000);
  const latestDataset = knowledge.getDataset(dataset.id);
  if (!latestDataset.active_release_id) {
    const releaseTask = knowledge.buildRelease(dataset.id, { activate: true });
    const release = await tasks.wait(releaseTask.id, config.localWorkspaceId, 30_000);
    if (release.status !== 'succeeded') throw new Error(`Release build failed: ${release.error_message}`);
  }
  console.log(JSON.stringify({ knowledgeBaseId: kb.id, datasetId: dataset.id, documentId: registered.document.id, duplicate: registered.duplicate, activeReleaseId: knowledge.getDataset(dataset.id).active_release_id }, null, 2));
  await app.close();
}

main().catch(error => {
  console.error('[ordo-seed] failed:', error);
  process.exit(1);
});
