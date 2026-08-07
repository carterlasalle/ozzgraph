/**
 * Dashboard configuration: runs-root directory, bind host, and port.
 *
 * Precedence: CLI flags > environment variables > defaults. The runs
 * root defaults to ``state`` (the kernel's default state dir), which is
 * resolved against the current working directory.
 */

import path from 'node:path';

export const DEFAULT_RUNS_DIR = 'state';
export const DEFAULT_HOST = '127.0.0.1';
export const DEFAULT_PORT = 8787;

export const RUNS_DIR_ENV = 'OZZGRAPH_DASHBOARD_RUNS_DIR';
export const HOST_ENV = 'OZZGRAPH_DASHBOARD_HOST';
export const PORT_ENV = 'OZZGRAPH_DASHBOARD_PORT';

export interface DashboardConfig {
  /** Absolute path of the runs root directory. */
  runsDir: string;
  /** Bind host. */
  host: string;
  /** Bind port (1-65535). */
  port: number;
}

export class ConfigError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'ConfigError';
  }
}

export const USAGE = `OzzGraph dashboard

Usage: node dist/main.js [options]

Options:
  --runs-dir <path>   Runs root directory (default: ${DEFAULT_RUNS_DIR}, or env ${RUNS_DIR_ENV})
  --host <host>       Bind host (default: ${DEFAULT_HOST}, or env ${HOST_ENV})
  --port <port>       Bind port (default: ${DEFAULT_PORT}, or env ${PORT_ENV})
  --help              Show this help

Environment:
  ${RUNS_DIR_ENV}   Runs root directory
  ${HOST_ENV}       Bind host
  ${PORT_ENV}       Bind port
`;

interface EnvLike {
  [key: string]: string | undefined;
}

function firstNonEmpty(...values: Array<string | undefined>): string | undefined {
  for (const value of values) {
    if (value !== undefined && value.length > 0) {
      return value;
    }
  }
  return undefined;
}

function parsePort(raw: string): number {
  if (!/^\d+$/.test(raw)) {
    throw new ConfigError(`invalid port ${JSON.stringify(raw)}; expected an integer 1-65535`);
  }
  const port = Number(raw);
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    throw new ConfigError(`invalid port ${raw}; expected an integer 1-65535`);
  }
  return port;
}

/**
 * Parse CLI argv (without the node/script prefix) and environment into a
 * validated {@link DashboardConfig}. CLI flags win over env vars, which
 * win over defaults.
 */
export function parseConfig(argv: string[], env: EnvLike): DashboardConfig {
  let runsDir: string | undefined;
  let host: string | undefined;
  let port: string | undefined;

  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i]!;
    const value = (): string => {
      const next = argv[i + 1];
      if (next === undefined) {
        throw new ConfigError(`missing value for ${arg}`);
      }
      i++;
      return next;
    };
    switch (arg) {
      case '--runs-dir':
        runsDir = value();
        break;
      case '--host':
        host = value();
        break;
      case '--port':
        port = value();
        break;
      case '--help':
      case '-h':
        throw new ConfigHelpRequested();
      default:
        throw new ConfigError(`unknown option ${JSON.stringify(arg)}`);
    }
  }

  const resolvedRunsDir = firstNonEmpty(runsDir, env[RUNS_DIR_ENV], DEFAULT_RUNS_DIR)!;
  const resolvedHost = firstNonEmpty(host, env[HOST_ENV], DEFAULT_HOST)!;
  const resolvedPort = parsePort(firstNonEmpty(port, env[PORT_ENV], String(DEFAULT_PORT))!);

  return {
    runsDir: path.resolve(resolvedRunsDir),
    host: resolvedHost,
    port: resolvedPort,
  };
}

/** Thrown when ``--help`` was requested; callers print {@link USAGE}. */
export class ConfigHelpRequested extends Error {
  constructor() {
    super('help requested');
    this.name = 'ConfigHelpRequested';
  }
}
