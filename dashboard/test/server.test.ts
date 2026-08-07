import { DatabaseSync } from 'node:sqlite';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import type { DashboardConfig } from '../src/config.js';
import { createDashboardServer } from '../src/server.js';

/**
 * End-to-end tests over the real HTTP server. The runs root is a temp
 * directory holding a realistic run: actions.jsonl (with malformed
 * lines), a kernel-shaped graph.db (built with node:sqlite), and an
 * artifacts directory.
 */

let tempDir: string;
let runsDir: string;
let baseUrl: string;
let closeServer: () => Promise<void>;

const KERNEL_FIXTURE = fs.readFileSync(
  fileURLToPath(new URL('./fixtures/kernel_events.jsonl', import.meta.url)),
  'utf8',
);

function graphEventLine(payload: Record<string, unknown>): string {
  return JSON.stringify({
    event_id: 'e',
    run_id: 'run-a',
    timestamp: '2026-08-06T15:15:00.123456Z',
    event_type: 'graph.entity_created',
    producer: 'test',
    schema_version: 1,
    payload,
  });
}

function buildRunDir(runRoot: string): void {
  const runDir = path.join(runRoot, 'run-a');
  fs.mkdirSync(runDir, { recursive: true });

  // Event log: two valid graph events + one malformed line + one non-graph event.
  const events = [
    graphEventLine({
      at: '2026-08-06T15:15:00.123456Z',
      entity_id: 'run-abc',
      entity_type: 'run',
      data: { status: 'active' },
    }),
    graphEventLine({
      at: '2026-08-06T15:15:00.123456Z',
      entity_id: 'action-1',
      entity_type: 'action',
      data: { command: 'curl -I http://x', duration: 1.0, confidence: 0.9 },
    }),
    'this line is not json',
    JSON.stringify({
      event_id: 'e4',
      run_id: 'run-a',
      timestamp: '2026-08-06T15:16:00Z',
      event_type: 'termination',
      producer: 'supervisor',
      schema_version: 1,
      payload: { reason: 'solved' },
    }),
  ];
  fs.writeFileSync(path.join(runDir, 'actions.jsonl'), events.join('\n') + '\n');

  // Kernel-shaped graph.db: entities + edges + user_version=2.
  const db = new DatabaseSync(path.join(runDir, 'graph.db'));
  db.exec(`
    CREATE TABLE entities (
      id TEXT PRIMARY KEY,
      type TEXT NOT NULL,
      data TEXT NOT NULL,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
    CREATE TABLE edges (
      id TEXT PRIMARY KEY,
      type TEXT NOT NULL,
      src_id TEXT NOT NULL,
      dst_id TEXT NOT NULL,
      data TEXT NOT NULL,
      created_at TEXT NOT NULL
    );
    PRAGMA user_version = 2;
  `);
  db.prepare(
    'INSERT INTO entities (id, type, data, created_at, updated_at) VALUES (?, ?, ?, ?, ?)',
  ).run(
    'run-abc',
    'run',
    '{"status":"active"}',
    '2026-08-06T15:15:00.123456+00:00',
    '2026-08-06T15:15:00.123456+00:00',
  );
  db.prepare(
    'INSERT INTO entities (id, type, data, created_at, updated_at) VALUES (?, ?, ?, ?, ?)',
  ).run(
    'action-1',
    'action',
    '{"command":"curl -I http://x","confidence":0.9,"duration":1.0}',
    '2026-08-06T15:15:00.123456+00:00',
    '2026-08-06T15:15:00.123456+00:00',
  );
  db.prepare(
    'INSERT INTO edges (id, type, src_id, dst_id, data, created_at) VALUES (?, ?, ?, ?, ?, ?)',
  ).run(
    'edge-1',
    'ACTION PRODUCED OBSERVATION',
    'action-1',
    'run-abc',
    '{"note":"caf\\u00e9"}',
    '2026-08-06T15:15:00.123456+00:00',
  );
  db.close();

  // Artifacts.
  const artifactsDir = path.join(runDir, 'artifacts');
  fs.mkdirSync(artifactsDir);
  fs.writeFileSync(path.join(artifactsDir, 'note.txt'), 'hello from the run\n');
  fs.writeFileSync(path.join(artifactsDir, 'blob.bin'), Buffer.from([0x00, 0x01, 0x02, 0xff]));

  // A second run with only events (no graph).
  const runB = path.join(runRoot, 'run-b');
  fs.mkdirSync(runB, { recursive: true });
  fs.writeFileSync(path.join(runB, 'actions.jsonl'), KERNEL_FIXTURE);
}

beforeEach(async () => {
  tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'ozzgraph-server-'));
  runsDir = path.join(tempDir, 'runs');
  fs.mkdirSync(runsDir);
  buildRunDir(runsDir);

  const config: DashboardConfig = { runsDir, host: '127.0.0.1', port: 0 };
  const server = createDashboardServer(config);
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  const address = server.address();
  if (address === null || typeof address === 'string') {
    throw new Error('server did not bind');
  }
  baseUrl = `http://127.0.0.1:${address.port}`;
  closeServer = () =>
    new Promise<void>((resolve, reject) =>
      server.close((error) => (error ? reject(error) : resolve())),
    );
});

afterEach(async () => {
  await closeServer();
  fs.rmSync(tempDir, { recursive: true, force: true });
});

async function getJson(urlPath: string): Promise<{ status: number; body: unknown }> {
  const res = await fetch(`${baseUrl}${urlPath}`);
  return { status: res.status, body: (await res.json()) as unknown };
}

describe('GET /api/runs', () => {
  it('lists discovered runs with metadata', async () => {
    const { status, body } = await getJson('/api/runs');
    expect(status).toBe(200);
    const runs = (body as { runs: Array<Record<string, unknown>> }).runs;
    expect(runs.map((run) => run['run_id'])).toEqual(['run-a', 'run-b']);
    const runA = runs.find((run) => run['run_id'] === 'run-a')!;
    expect(runA['event_count']).toBe(3); // 2 valid + 1 termination; malformed skipped
    expect(runA['artifact_count']).toBe(2);
    expect(runA['has_graph']).toBe(true);
    expect(runA['has_events']).toBe(true);
    expect(typeof runA['last_modified']).toBe('string');
  });
});

describe('GET /api/runs/{run_id}', () => {
  it('returns the run summary', async () => {
    const { status, body } = await getJson('/api/runs/run-a');
    expect(status).toBe(200);
    expect((body as Record<string, unknown>)['run_id']).toBe('run-a');
    expect((body as Record<string, unknown>)['event_count']).toBe(3);
  });

  it('404s for an unknown run', async () => {
    const { status, body } = await getJson('/api/runs/nope');
    expect(status).toBe(404);
    expect(body).toEqual({
      error: { code: 'run_not_found', message: expect.any(String) },
    });
  });
});

describe('GET /api/runs/{run_id}/graph', () => {
  it('returns entities, edges, schema version, and a kernel-consistent hash', async () => {
    const { status, body } = await getJson('/api/runs/run-a/graph');
    expect(status).toBe(200);
    const graph = body as {
      run_id: string;
      schema_version: number;
      entities: Array<Record<string, unknown>>;
      edges: Array<Record<string, unknown>>;
      entity_count: number;
      edge_count: number;
      graph_hash: string;
    };
    expect(graph.run_id).toBe('run-a');
    expect(graph.schema_version).toBe(2);
    expect(graph.entity_count).toBe(2);
    expect(graph.edge_count).toBe(1);
    expect(graph.graph_hash).toMatch(/^[0-9a-f]{64}$/);
    const action = graph.entities.find((entity) => entity['id'] === 'action-1')!;
    expect(action['type']).toBe('action');
    expect(action['data']).toEqual({
      command: 'curl -I http://x',
      confidence: 0.9,
      duration: 1.0,
    });
    expect(graph.edges[0]).toMatchObject({
      id: 'edge-1',
      type: 'ACTION PRODUCED OBSERVATION',
      src_id: 'action-1',
      dst_id: 'run-abc',
    });
  });

  it('404s when the run has no graph.db', async () => {
    const { status, body } = await getJson('/api/runs/run-b/graph');
    expect(status).toBe(404);
    expect((body as { error: { code: string } }).error.code).toBe('graph_not_found');
  });

  it('404s for an unknown run', async () => {
    const { status, body } = await getJson('/api/runs/nope/graph');
    expect(status).toBe(404);
    expect((body as { error: { code: string } }).error.code).toBe('run_not_found');
  });

  it('never writes to the kernel database (read-only)', async () => {
    const dbPath = path.join(runsDir, 'run-a', 'graph.db');
    const before = fs.readFileSync(dbPath);
    await getJson('/api/runs/run-a/graph');
    const after = fs.readFileSync(dbPath);
    expect(after.equals(before)).toBe(true);

    const db = new DatabaseSync(dbPath, { readOnly: true });
    expect(() => db.exec('CREATE TABLE hacked (x TEXT)')).toThrow();
    db.close();
  });
});

describe('GET /api/runs/{run_id}/events', () => {
  it('returns parseable events and reports skipped malformed lines', async () => {
    const { status, body } = await getJson('/api/runs/run-a/events');
    expect(status).toBe(200);
    const payload = body as {
      run_id: string;
      event_count: number;
      skipped: number;
      events: Array<Record<string, unknown>>;
    };
    expect(payload.run_id).toBe('run-a');
    expect(payload.event_count).toBe(3);
    expect(payload.skipped).toBe(1);
    expect(payload.events.map((event) => event['event_type'])).toEqual([
      'graph.entity_created',
      'graph.entity_created',
      'termination',
    ]);
  });
});

describe('GET /api/runs/{run_id}/artifacts/{artifact_id}', () => {
  it('serves artifact bytes with a proper content type', async () => {
    const res = await fetch(`${baseUrl}/api/runs/run-a/artifacts/note.txt`);
    expect(res.status).toBe(200);
    expect(res.headers.get('content-type')).toContain('text/plain');
    expect(await res.text()).toBe('hello from the run\n');
  });

  it('serves binary artifacts as octet-stream', async () => {
    const res = await fetch(`${baseUrl}/api/runs/run-a/artifacts/blob.bin`);
    expect(res.status).toBe(200);
    expect(res.headers.get('content-type')).toBe('application/octet-stream');
    expect(Buffer.from(await res.arrayBuffer())).toEqual(
      Buffer.from([0x00, 0x01, 0x02, 0xff]),
    );
  });

  it('404s for an unknown artifact', async () => {
    const { status, body } = await getJson('/api/runs/run-a/artifacts/nope.txt');
    expect(status).toBe(404);
    expect((body as { error: { code: string } }).error.code).toBe('artifact_not_found');
  });

  it('rejects traversal in artifact ids', async () => {
    const { status, body } = await getJson('/api/runs/run-a/artifacts/..%2F..%2Fsecret');
    expect(status).toBe(400);
    expect((body as { error: { code: string } }).error.code).toBe('invalid_artifact_id');
  });
});

describe('GET /api/runs/{run_id}/metrics', () => {
  it('derives metrics from events', async () => {
    const { status, body } = await getJson('/api/runs/run-a/metrics');
    expect(status).toBe(200);
    const metrics = body as Record<string, unknown>;
    expect(metrics['run_id']).toBe('run-a');
    expect(metrics['total_events']).toBe(3);
    expect(metrics['skipped']).toBe(1);
    expect(metrics['action_count']).toBe(1);
    expect(metrics['event_type_counts']).toEqual({
      'graph.entity_created': 2,
      termination: 1,
    });
  });
});

describe('POST /api/runs/{run_id}/replay', () => {
  it('replays the log and returns counts plus a stable hash', async () => {
    const res = await fetch(`${baseUrl}/api/runs/run-b/replay`, { method: 'POST' });
    expect(res.status).toBe(200);
    const result = (await res.json()) as Record<string, unknown>;
    expect(result['event_count']).toBe(6);
    expect(result['entity_count']).toBe(2);
    expect(result['edge_count']).toBe(1);
    expect(result['graph_hash']).toBe(
      '4d3ec0bc241bd4756eb46ef330a79aad9a627069210313331a2e13fe2271d315',
    );

    // Deterministic across calls.
    const res2 = await fetch(`${baseUrl}/api/runs/run-b/replay`, { method: 'POST' });
    expect(((await res2.json()) as Record<string, unknown>)['graph_hash']).toBe(
      result['graph_hash'],
    );
  });

  it('400s on a malformed replay (invalid event line)', async () => {
    const res = await fetch(`${baseUrl}/api/runs/run-a/replay`, { method: 'POST' });
    expect(res.status).toBe(400);
    const body = (await res.json()) as { error: { code: string; message: string } };
    expect(body.error.code).toBe('replay_malformed_event');
    expect(body.error.message).toContain('line 3');
  });

  it('400s on a malformed JSON request body', async () => {
    const res = await fetch(`${baseUrl}/api/runs/run-b/replay`, {
      method: 'POST',
      body: 'not json at all',
    });
    expect(res.status).toBe(400);
    expect(((await res.json()) as { error: { code: string } }).error.code).toBe(
      'invalid_json_body',
    );
  });

  it('404s for an unknown run', async () => {
    const res = await fetch(`${baseUrl}/api/runs/nope/replay`, { method: 'POST' });
    expect(res.status).toBe(404);
  });
});

describe('error handling and security', () => {
  it('rejects traversal in run ids with 400', async () => {
    const { status, body } = await getJson('/api/runs/..%2F..%2Fetc/graph');
    expect(status).toBe(400);
    expect((body as { error: { code: string } }).error.code).toBe('invalid_run_id');
  });

  it('rejects absolute paths in run ids', async () => {
    const { status } = await getJson('/api/runs/%2Fetc%2Fpasswd/events');
    expect(status).toBe(400);
  });

  it('404s for unknown routes', async () => {
    const { status, body } = await getJson('/api/unknown');
    expect(status).toBe(404);
    expect((body as { error: { code: string } }).error.code).toBe('route_not_found');
  });

  it('405s for wrong methods with an error body', async () => {
    const res = await fetch(`${baseUrl}/api/runs`, { method: 'PUT' });
    expect(res.status).toBe(405);
    expect(((await res.json()) as { error: { code: string } }).error.code).toBe(
      'method_not_allowed',
    );
  });

  it('never leaks stack traces', async () => {
    const { status, body } = await getJson('/api/runs/nope');
    const raw = JSON.stringify(body);
    expect(status).toBe(404);
    expect(raw).not.toContain('at ');
    expect(raw).not.toContain('node:');
  });

  it('serves healthz', async () => {
    const { status, body } = await getJson('/healthz');
    expect(status).toBe(200);
    expect(body).toEqual({ ok: true });
  });
});

describe('root-as-run layout', () => {
  it('serves the root run under its basename id', async () => {
    // Turn the runs root itself into a single run (kernel default layout).
    fs.writeFileSync(path.join(runsDir, 'actions.jsonl'), KERNEL_FIXTURE);
    const rootId = path.basename(runsDir);

    const list = await getJson('/api/runs');
    const runs = (list.body as { runs: Array<Record<string, unknown>> }).runs;
    expect(runs.some((run) => run['run_id'] === rootId)).toBe(true);

    const detail = await getJson(`/api/runs/${rootId}`);
    expect(detail.status).toBe(200);
    expect((detail.body as Record<string, unknown>)['event_count']).toBe(6);

    const replay = await fetch(`${baseUrl}/api/runs/${rootId}/replay`, { method: 'POST' });
    expect(replay.status).toBe(200);
    expect(((await replay.json()) as Record<string, unknown>)['graph_hash']).toBe(
      '4d3ec0bc241bd4756eb46ef330a79aad9a627069210313331a2e13fe2271d315',
    );
  });
});
