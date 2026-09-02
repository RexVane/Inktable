#!/usr/bin/env node
'use strict';

const { createApp } = require('./app');

async function main() {
  const app = await createApp();
  const { host, port, dataRoot } = app.services.config;
  await app.listen({ host, port });
  app.log.info({ dataRoot }, 'Ordo product server started');
  app.log.info(`Open http://${host}:${port}/`);
}

main().catch(error => {
  console.error('[ordo] startup failed:', error);
  process.exit(1);
});
