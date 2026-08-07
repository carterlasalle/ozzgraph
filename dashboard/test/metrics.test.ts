import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { deriveMetrics } from '../src/metrics.js';

let tempDir: string;

beforeEach(() => {
  tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'ozzgraph-metrics-'));
});

afterEach(() => {
  fs.rmSync(tempDir, { recursive: true, force: true });
});

function logPath(content: string): string {
  const file = path.join(tempDir, 'actions.jsonl');
  fs.writeFileSync(file, content);
  return file;
}

function event(
  eventType: string,
  producer: string,
  timestamp: string,
  payload: Record<string, unknown> = {},
): string {
  return JSON.stringify({
    event_id: 'e',
    run_id: 'run-x',
    timestamp,
    event_type: eventType,
    producer,
    schema_version: 1,
    payload,
  });
}

describe('deriveMetrics', () => {
  it('counts event types, producers, actions, and model calls', () => {
    const log = [
      event('bootstrap.targets_parsed', 'bootstrap', '2026-08-06T15:15:00Z', { count: 1 }),
      event(
        'graph.entity_created',
        'executor',
        '2026-08-06T15:15:01Z',
        { entity_id: 'a1', entity_type: 'action', data: {} },
      ),
      event(
        'graph.entity_created',
        'model_client',
        '2026-08-06T15:15:02Z',
        { entity_id: 'm1', entity_type: 'model_call', data: {} },
      ),
      event(
        'graph.entity_created',
        'executor',
        '2026-08-06T15:15:03Z',
        { entity_id: 'a2', entity_type: 'action', data: {} },
      ),
      event('termination', 'supervisor', '2026-08-06T15:16:00Z', { reason: 'solved' }),
    ].join('\n') + '\n';

    const metrics = deriveMetrics('run-x', logPath(log));
    expect(metrics.run_id).toBe('run-x');
    expect(metrics.total_events).toBe(5);
    expect(metrics.skipped).toBe(0);
    expect(metrics.event_type_counts).toEqual({
      'bootstrap.targets_parsed': 1,
      'graph.entity_created': 3,
      termination: 1,
    });
    expect(metrics.producer_counts).toEqual({
      bootstrap: 1,
      executor: 2,
      model_client: 1,
      supervisor: 1,
    });
    expect(metrics.action_count).toBe(2);
    expect(metrics.model_call_count).toBe(1);
    expect(metrics.first_event_at).toBe('2026-08-06T15:15:00.000Z');
    expect(metrics.last_event_at).toBe('2026-08-06T15:16:00.000Z');
    expect(metrics.duration_ms).toBe(60_000);
  });

  it('skips malformed lines and reports them', () => {
    const log = [event('termination', 'supervisor', '2026-08-06T15:16:00Z'), 'garbage', ''].join(
      '\n',
    ) + '\n';
    const metrics = deriveMetrics('run-x', logPath(log));
    expect(metrics.total_events).toBe(1);
    expect(metrics.skipped).toBe(1);
  });

  it('handles missing files and empty logs', () => {
    const metrics = deriveMetrics('run-x', path.join(tempDir, 'nope.jsonl'));
    expect(metrics.total_events).toBe(0);
    expect(metrics.skipped).toBe(0);
    expect(metrics.event_type_counts).toEqual({});
    expect(metrics.first_event_at).toBeNull();
    expect(metrics.last_event_at).toBeNull();
    expect(metrics.duration_ms).toBeNull();
  });
});
