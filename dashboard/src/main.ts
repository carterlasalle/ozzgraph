#!/usr/bin/env node
/**
 * OzzGraph dashboard entrypoint. See docs/API_AND_INTEGRATIONS.md
 * ("Optional Dashboard API") for setup and configuration.
 */

import { ConfigError, ConfigHelpRequested, USAGE, parseConfig } from './config.js';
import { createDashboardServer } from './server.js';

let config;
try {
  config = parseConfig(process.argv.slice(2), process.env);
} catch (error) {
  if (error instanceof ConfigHelpRequested) {
    process.stdout.write(USAGE);
    process.exit(0);
  }
  if (error instanceof ConfigError) {
    process.stderr.write(`error: ${error.message}\n\n${USAGE}`);
    process.exit(2);
  }
  throw error;
}

const server = createDashboardServer(config);
server.listen(config.port, config.host, () => {
  console.log(`[dashboard] listening on http://${config.host}:${config.port}`);
  console.log(`[dashboard] runs root: ${config.runsDir}`);
});

let shuttingDown = false;
function shutdown(signal: string): void {
  if (shuttingDown) {
    return;
  }
  shuttingDown = true;
  console.log(`[dashboard] received ${signal}, shutting down`);
  server.close(() => process.exit(0));
  // Force-exit if the close never completes.
  setTimeout(() => process.exit(0), 5000).unref();
}
process.on('SIGINT', () => shutdown('SIGINT'));
process.on('SIGTERM', () => shutdown('SIGTERM'));
