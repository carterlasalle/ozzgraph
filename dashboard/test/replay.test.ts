import { createHash } from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { KERNEL_SCHEMA_VERSION, replayEvents } from '../src/replay.js';

/**
 * Golden values produced by the REAL kernel (src/ozzgraph/replay.py) on
 * the committed fixture — see /tmp/gen_golden_fixture.py. This proves
 * the TypeScript port reproduces the kernel's hash byte-for-byte.
 */
const FIXTURE_PATH = fileURLToPath(new URL('./fixtures/kernel_events.jsonl', import.meta.url));
const KERNEL_EXPECTED = {
  event_count: 6,
  entity_count: 2,
  edge_count: 1,
  graph_hash: '4d3ec0bc241bd4756eb46ef330a79aad9a627069210313331a2e13fe2271d315',
};

/** sha256("schema_version=2\n") — the kernel's empty-graph hash. */
function emptyGraphHash(): string {
  return createHash('sha256')
    .update(`schema_version=${KERNEL_SCHEMA_VERSION}\n`)
    .digest('hex');
}

let tempDir: string;

beforeEach(() => {
  tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'ozzgraph-replay-'));
});

afterEach(() => {
  fs.rmSync(tempDir, { recursive: true, force: true });
});

function logPath(content: string): string {
  const file = path.join(tempDir, 'actions.jsonl');
  fs.writeFileSync(file, content);
  return file;
}

function line(eventType: string, payload: Record<string, unknown>): string {
  return JSON.stringify({
    event_id: 'e',
    run_id: 'run-x',
    timestamp: '2026-08-06T15:15:00Z',
    event_type: eventType,
    producer: 'test',
    schema_version: 1,
    payload,
  });
}

describe('replayEvents — kernel cross-check', () => {
  it('matches the kernel hash, counts, and determinism on the golden fixture', () => {
    const first = replayEvents(FIXTURE_PATH);
    expect(first).toEqual(KERNEL_EXPECTED);

    // Determinism: replaying the same file twice yields the same hash.
    const second = replayEvents(FIXTURE_PATH);
    expect(second.graph_hash).toBe(first.graph_hash);
  });

  it('replays an empty/graph-free log to the stable empty-graph hash', () => {
    const result = replayEvents(logPath(''));
    expect(result).toEqual({
      event_count: 0,
      entity_count: 0,
      edge_count: 0,
      graph_hash: emptyGraphHash(),
    });

    // A log with only non-graph events also yields the empty-graph hash.
    const nonGraph = replayEvents(
      logPath(line('bootstrap.targets_parsed', { count: 1 }) + '\n'),
    );
    expect(nonGraph.event_count).toBe(1);
    expect(nonGraph.entity_count).toBe(0);
    expect(nonGraph.edge_count).toBe(0);
    expect(nonGraph.graph_hash).toBe(emptyGraphHash());
  });

  it('a missing file replays to the empty graph', () => {
    const result = replayEvents(path.join(tempDir, 'nope.jsonl'));
    expect(result.graph_hash).toBe(emptyGraphHash());
  });
});

describe('replayEvents — semantics', () => {
  it('applies create/update/delete and cascade-deletes edges', () => {
    const log = [
      line('graph.entity_created', {
        entity_id: 'e1',
        entity_type: 'run',
        data: { status: 'active' },
        at: '2026-08-06T15:15:00Z',
      }),
      line('graph.entity_created', {
        entity_id: 'e2',
        entity_type: 'action',
        data: {},
        at: '2026-08-06T15:15:00Z',
      }),
      line('graph.edge_created', {
        edge_id: 'edge1',
        edge_type: 'X Y',
        src_id: 'e2',
        dst_id: 'e1',
        data: {},
        at: '2026-08-06T15:15:00Z',
      }),
      line('graph.entity_updated', {
        entity_id: 'e1',
        data: { status: 'done' },
        at: '2026-08-06T15:16:00Z',
      }),
      // Deleting e2 cascade-deletes edge1 (kernel ON DELETE CASCADE).
      line('graph.entity_deleted', { entity_id: 'e2' }),
    ].join('\n') + '\n';
    const result = replayEvents(logPath(log));
    expect(result.event_count).toBe(5);
    expect(result.entity_count).toBe(1);
    expect(result.edge_count).toBe(0);
  });

  it('ignores unknown event types', () => {
    const log =
      line('some.future.event', { anything: true }) + '\n' + line('termination', {}) + '\n';
    const result = replayEvents(logPath(log));
    expect(result.event_count).toBe(2);
    expect(result.entity_count).toBe(0);
    expect(result.graph_hash).toBe(emptyGraphHash());
  });

  it('is sensitive to event order (create/update do not commute)', () => {
    const mk = (initial: Record<string, unknown>, update: Record<string, unknown>): string =>
      [
        line('graph.entity_created', {
          entity_id: 'e1',
          entity_type: 'run',
          data: initial,
          at: '2026-08-06T15:15:00Z',
        }),
        line('graph.entity_updated', {
          entity_id: 'e1',
          data: update,
          at: '2026-08-06T15:16:00Z',
        }),
      ].join('\n');
    const ab = replayEvents(logPath(mk({ status: 'active' }, { status: 'done' }) + '\n'));
    const ba = replayEvents(logPath(mk({ status: 'done' }, { status: 'active' }) + '\n'));
    expect(ab.entity_count).toBe(1);
    expect(ab.graph_hash).not.toBe(ba.graph_hash);
  });

  it('is sensitive to payload changes (raw number literals distinguish 1 vs 1.0)', () => {
    const mk = (dataLiteral: string): string =>
      `{"event_id":"e","run_id":"run-x","timestamp":"2026-08-06T15:15:00Z",` +
      `"event_type":"graph.entity_created","producer":"test","schema_version":1,` +
      `"payload":{"at":"2026-08-06T15:15:00Z","entity_id":"e1","entity_type":"run","data":${dataLiteral}}}`;
    const h1 = replayEvents(logPath(mk('{"a":1}') + '\n')).graph_hash;
    const h2 = replayEvents(logPath(mk('{"a":1.0}') + '\n')).graph_hash;
    expect(h1).not.toBe(h2);
  });
});

describe('replayEvents — malformed input aborts loudly (kernel mirror)', () => {
  function expectMalformed(content: string, lineNumber: number): void {
    expect(() => replayEvents(logPath(content))).toThrowError(
      expect.objectContaining({ line: lineNumber }),
    );
  }

  it('aborts on invalid JSON with the line number', () => {
    expectMalformed('not json\n', 1);
  });

  it('aborts on a non-object event line', () => {
    expectMalformed('[1,2,3]\n', 1);
  });

  it('aborts on a graph event with a non-object payload', () => {
    expectMalformed(line('graph.entity_created', {} as never).replace('"payload":{}', '"payload":"x"') + '\n', 1);
  });

  it('aborts on a missing payload field', () => {
    expectMalformed(
      line('graph.entity_created', {
        entity_type: 'run',
        data: {},
        at: '2026-08-06T15:15:00Z',
      }) + '\n',
      1,
    );
  });

  it('aborts on a naive (offset-less) timestamp', () => {
    expectMalformed(
      line('graph.entity_created', {
        entity_id: 'e1',
        entity_type: 'run',
        data: {},
        at: '2026-08-06T15:15:00',
      }) + '\n',
      1,
    );
  });

  it('aborts on duplicate entity creation', () => {
    const mk = line('graph.entity_created', {
      entity_id: 'e1',
      entity_type: 'run',
      data: {},
      at: '2026-08-06T15:15:00Z',
    });
    expectMalformed(`${mk}\n${mk}\n`, 2);
  });

  it('aborts on updating a missing entity', () => {
    expectMalformed(
      line('graph.entity_updated', {
        entity_id: 'nope',
        data: {},
        at: '2026-08-06T15:15:00Z',
      }) + '\n',
      1,
    );
  });

  it('aborts on updating without a data field', () => {
    expectMalformed(
      line('graph.entity_created', {
        entity_id: 'e1',
        entity_type: 'run',
        data: {},
        at: '2026-08-06T15:15:00Z',
      }) +
        '\n' +
        line('graph.entity_updated', {
          entity_id: 'e1',
          at: '2026-08-06T15:15:00Z',
        }) +
        '\n',
      2,
    );
  });

  it('aborts on an edge whose endpoints do not exist', () => {
    expectMalformed(
      line('graph.edge_created', {
        edge_id: 'edge1',
        edge_type: 'X Y',
        src_id: 'missing',
        dst_id: 'missing2',
        data: {},
        at: '2026-08-06T15:15:00Z',
      }) + '\n',
      1,
    );
  });

  it('aborts on a duplicate edge triple', () => {
    const createEntities =
      line('graph.entity_created', {
        entity_id: 'e1',
        entity_type: 'run',
        data: {},
        at: '2026-08-06T15:15:00Z',
      }) +
      '\n' +
      line('graph.entity_created', {
        entity_id: 'e2',
        entity_type: 'run',
        data: {},
        at: '2026-08-06T15:15:00Z',
      }) +
      '\n';
    const edge = line('graph.edge_created', {
      edge_id: 'edge1',
      edge_type: 'X Y',
      src_id: 'e1',
      dst_id: 'e2',
      data: {},
      at: '2026-08-06T15:15:00Z',
    });
    expectMalformed(createEntities + `${edge}\n${edge}\n`, 4);
  });

  it('aborts on deleting a missing entity or edge', () => {
    expectMalformed(line('graph.entity_deleted', { entity_id: 'nope' }) + '\n', 1);
    expectMalformed(line('graph.edge_deleted', { edge_id: 'nope' }) + '\n', 1);
  });
});
