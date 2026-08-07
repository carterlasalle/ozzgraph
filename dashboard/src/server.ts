/**
 * The OzzGraph dashboard HTTP server (node:http, zero runtime deps).
 *
 * Endpoints (documented in docs/API_AND_INTEGRATIONS.md):
 *
 * - GET  /api/runs
 * - GET  /api/runs/{run_id}
 * - GET  /api/runs/{run_id}/graph
 * - GET  /api/runs/{run_id}/events
 * - GET  /api/runs/{run_id}/artifacts/{artifact_id}
 * - GET  /api/runs/{run_id}/metrics
 * - POST /api/runs/{run_id}/replay
 * - GET  /healthz
 *
 * All JSON responses use the error shape
 * ``{"error": {"code", "message"}}``; details are logged to stderr and
 * never returned to the client (no stack-trace leakage). Path segments
 * are validated strictly (see ``pathguard.ts``).
 */

import { createServer, type IncomingMessage, type Server, type ServerResponse } from 'node:http';
import path from 'node:path';
import { readFileSync } from 'node:fs';

import type { DashboardConfig } from './config.js';
import { ApiError, type ErrorCode } from './errors.js';
import { isSafeIdentifier } from './pathguard.js';
import { discoverRuns, lookupRun, resolveRunDir } from './runDiscovery.js';
import { readEventsFile } from './eventsLog.js';
import { readGraph, type GraphRead } from './sqliteGraph.js';
import { replayEvents, ReplayMalformedError } from './replay.js';
import { deriveMetrics } from './metrics.js';

const MAX_BODY_BYTES = 1024 * 1024;

const CONTENT_TYPES: Record<string, string> = {
  '.json': 'application/json',
  '.txt': 'text/plain; charset=utf-8',
  '.log': 'text/plain; charset=utf-8',
  '.md': 'text/markdown; charset=utf-8',
  '.csv': 'text/csv; charset=utf-8',
  '.html': 'text/html; charset=utf-8',
  '.htm': 'text/html; charset=utf-8',
  '.xml': 'application/xml',
  '.yaml': 'application/yaml',
  '.yml': 'application/yaml',
  '.toml': 'application/toml',
  '.py': 'text/x-python; charset=utf-8',
  '.sh': 'text/x-shellscript; charset=utf-8',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.gif': 'image/gif',
  '.svg': 'image/svg+xml',
  '.webp': 'image/webp',
  '.pdf': 'application/pdf',
  '.zip': 'application/zip',
  '.gz': 'application/gzip',
  '.bin': 'application/octet-stream',
};

function contentTypeFor(fileName: string): string {
  const ext = path.extname(fileName).toLowerCase();
  return CONTENT_TYPES[ext] ?? 'application/octet-stream';
}

function sendJson(res: ServerResponse, status: number, body: unknown): void {
  const raw = JSON.stringify(body);
  res.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'content-length': Buffer.byteLength(raw),
    'x-content-type-options': 'nosniff',
    'cache-control': 'no-store',
  });
  res.end(raw);
}

function sendError(res: ServerResponse, status: number, code: ErrorCode, message: string): void {
  sendJson(res, status, { error: { code, message } });
}

/** Resolve the run dir for a validated request, or throw a 404. */
function requireRun(runsDir: string, runId: string): string {
  const dir = resolveRunDir(runsDir, runId);
  if (dir === null) {
    throw new ApiError(404, 'run_not_found', `unknown run '${runId}'`);
  }
  return dir;
}

/** Route one request; throws {@link ApiError} for client errors. */
async function handleRequest(
  config: DashboardConfig,
  method: string,
  segments: string[],
  body: Buffer,
): Promise<unknown> {
  // GET /healthz
  if (segments.length === 1 && segments[0] === 'healthz' && method === 'GET') {
    return { ok: true };
  }
  if (segments[0] !== 'api' || segments[1] !== 'runs') {
    throw new ApiError(404, 'route_not_found', 'unknown route');
  }
  const rest = segments.slice(2);

  if (rest.length === 0) {
    if (method !== 'GET') {
      throw new ApiError(405, 'method_not_allowed', 'method not allowed');
    }
    return { runs: discoverRuns(config.runsDir) };
  }

  const [runId, ...tail] = rest;
  if (!isSafeIdentifier(runId!)) {
    throw new ApiError(400, 'invalid_run_id', `invalid run id '${runId}'`);
  }

  // GET /api/runs/{run_id}
  if (tail.length === 0) {
    if (method !== 'GET') {
      throw new ApiError(405, 'method_not_allowed', 'method not allowed');
    }
    const resolved = lookupRun(config.runsDir, runId!);
    if (resolved === null) {
      throw new ApiError(404, 'run_not_found', `unknown run '${runId}'`);
    }
    return resolved.summary;
  }

  const [resource, ...resourceTail] = tail;

  switch (resource) {
    case 'graph': {
      if (method !== 'GET') {
        throw new ApiError(405, 'method_not_allowed', 'method not allowed');
      }
      if (resourceTail.length !== 0) {
        throw new ApiError(404, 'route_not_found', 'unknown route');
      }
      const runDir = requireRun(config.runsDir, runId!);
      let graph: GraphRead | null;
      try {
        graph = readGraph(path.join(runDir, 'graph.db'));
      } catch {
        throw new ApiError(
          500,
          'graph_read_failed',
          `could not read graph for run '${runId}'`,
        );
      }
      if (graph === null) {
        throw new ApiError(404, 'graph_not_found', `run '${runId}' has no graph.db`);
      }
      return {
        run_id: runId,
        schema_version: graph.schema_version,
        entities: graph.entities.map((entity) => ({
          id: entity.id,
          type: entity.type,
          data: JSON.parse(entity.data),
          created_at: entity.created_at,
          updated_at: entity.updated_at,
        })),
        edges: graph.edges.map((edge) => ({
          id: edge.id,
          type: edge.type,
          src_id: edge.src_id,
          dst_id: edge.dst_id,
          data: JSON.parse(edge.data),
          created_at: edge.created_at,
        })),
        entity_count: graph.entity_count,
        edge_count: graph.edge_count,
        graph_hash: graph.graph_hash,
      };
    }
    case 'events': {
      if (method !== 'GET') {
        throw new ApiError(405, 'method_not_allowed', 'method not allowed');
      }
      if (resourceTail.length !== 0) {
        throw new ApiError(404, 'route_not_found', 'unknown route');
      }
      const runDir = requireRun(config.runsDir, runId!);
      const file = readEventsFile(path.join(runDir, 'actions.jsonl'));
      return {
        run_id: runId,
        event_count: file.eventCount,
        skipped: file.skipped,
        events: file.events,
      };
    }
    case 'artifacts': {
      if (method !== 'GET') {
        throw new ApiError(405, 'method_not_allowed', 'method not allowed');
      }
      const artifactId = resourceTail[0];
      if (resourceTail.length !== 1 || !isSafeIdentifier(artifactId!)) {
        throw new ApiError(
          400,
          'invalid_artifact_id',
          `invalid artifact id '${artifactId}'`,
        );
      }
      const runDir = requireRun(config.runsDir, runId!);
      const artifactPath = path.join(runDir, 'artifacts', artifactId!);
      let content: Buffer;
      try {
        content = readFileSync(artifactPath);
      } catch (error) {
        const code = (error as NodeJS.ErrnoException).code;
        if (code === 'ENOENT' || code === 'EISDIR') {
          throw new ApiError(
            404,
            'artifact_not_found',
            `unknown artifact '${artifactId}' in run '${runId}'`,
          );
        }
        throw new ApiError(
          500,
          'internal_error',
          `could not read artifact '${artifactId}'`,
        );
      }
      return { __artifact: { content, contentType: contentTypeFor(artifactId!) } };
    }
    case 'metrics': {
      if (method !== 'GET') {
        throw new ApiError(405, 'method_not_allowed', 'method not allowed');
      }
      if (resourceTail.length !== 0) {
        throw new ApiError(404, 'route_not_found', 'unknown route');
      }
      const runDir = requireRun(config.runsDir, runId!);
      return deriveMetrics(runId!, path.join(runDir, 'actions.jsonl'));
    }
    case 'replay': {
      if (method !== 'POST') {
        throw new ApiError(405, 'method_not_allowed', 'method not allowed');
      }
      if (resourceTail.length !== 0) {
        throw new ApiError(404, 'route_not_found', 'unknown route');
      }
      const runDir = requireRun(config.runsDir, runId!);
      if (body.length > 0) {
        try {
          JSON.parse(body.toString('utf8'));
        } catch {
          throw new ApiError(400, 'invalid_json_body', 'request body is not valid JSON');
        }
      }
      try {
        return replayEvents(path.join(runDir, 'actions.jsonl'));
      } catch (error) {
        if (error instanceof ReplayMalformedError) {
          throw new ApiError(
            400,
            'replay_malformed_event',
            `line ${error.line}: ${error.message}`,
          );
        }
        throw error;
      }
    }
    default:
      throw new ApiError(404, 'route_not_found', 'unknown route');
  }
}

/** Create the dashboard HTTP server for a validated config. */
export function createDashboardServer(config: DashboardConfig): Server {
  return createServer((req, res) => {
    void (async () => {
      try {
        const url = new URL(req.url ?? '/', `http://${req.headers.host ?? 'localhost'}`);
        let decodedPath: string;
        try {
          decodedPath = decodeURIComponent(url.pathname);
        } catch {
          sendError(res, 400, 'invalid_run_id', 'malformed percent-encoding in path');
          return;
        }
        // Reject encoded separators and doubled slashes (encoded absolute
        // paths, e.g. %2Fetc%2Fpasswd, decode to '//').
        if (decodedPath.includes('//')) {
          sendError(res, 400, 'invalid_run_id', 'path must not contain encoded separators');
          return;
        }
        const segments = decodedPath
          .split('/')
          .filter((segment) => segment.length > 0);
        const body = await readBody(req);
        const result = await handleRequest(config, req.method ?? 'GET', segments, body);
        if (
          result !== null &&
          typeof result === 'object' &&
          '__artifact' in result
        ) {
          const artifact = (result as { __artifact: { content: Buffer; contentType: string } })
            .__artifact;
          res.writeHead(200, {
            'content-type': artifact.contentType,
            'content-length': artifact.content.length,
            'x-content-type-options': 'nosniff',
            'cache-control': 'no-store',
          });
          res.end(artifact.content);
          return;
        }
        sendJson(res, 200, result);
      } catch (error) {
        if (error instanceof ApiError) {
          sendError(res, error.status, error.code, error.message);
          return;
        }
        // Never leak internals to the client.
        console.error(`[dashboard] unhandled error: ${error instanceof Error ? error.stack : String(error)}`);
        sendError(res, 500, 'internal_error', 'internal server error');
      }
    })();
  });
}

async function readBody(req: IncomingMessage): Promise<Buffer> {
  const chunks: Buffer[] = [];
  let total = 0;
  for await (const chunk of req) {
    total += chunk.length;
    if (total > MAX_BODY_BYTES) {
      throw new ApiError(413, 'payload_too_large', 'request body too large');
    }
    chunks.push(chunk);
  }
  return Buffer.concat(chunks);
}
