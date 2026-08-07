import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { discoverRuns, lookupRun, resolveRunDir, summarizeRun } from '../src/runDiscovery.js';

let tempDir: string;

beforeEach(() => {
  tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'ozzgraph-runs-'));
});

afterEach(() => {
  fs.rmSync(tempDir, { recursive: true, force: true });
});

function writeRun(runId: string, events: string[], withGraph = false): void {
  const dir = path.join(tempDir, runId);
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, 'actions.jsonl'), events.join('\n') + '\n');
  if (withGraph) {
    fs.writeFileSync(path.join(dir, 'graph.db'), 'not really sqlite');
  }
}

function events(count: number): string[] {
  const out: string[] = [];
  for (let i = 0; i < count; i++) {
    out.push(JSON.stringify({ event_id: `e${i}`, event_type: 'x.y', payload: {} }));
  }
  return out;
}

describe('run discovery', () => {
  it('discovers subdirectories with actions.jsonl and/or graph.db, sorted by id', () => {
    writeRun('run-b', events(2));
    writeRun('run-a', events(1), true);
    fs.mkdirSync(path.join(tempDir, 'not-a-run'));
    fs.mkdirSync(path.join(tempDir, '.hidden-run'));
    fs.writeFileSync(path.join(tempDir, '.hidden-run', 'actions.jsonl'), '{}');

    const runs = discoverRuns(tempDir);
    expect(runs.map((run) => run.run_id)).toEqual(['run-a', 'run-b']);
    const runA = runs.find((run) => run.run_id === 'run-a')!;
    expect(runA.event_count).toBe(1);
    expect(runA.has_graph).toBe(true);
    expect(runA.has_events).toBe(true);
    expect(runA.artifact_count).toBe(0);
    const runB = runs.find((run) => run.run_id === 'run-b')!;
    expect(runB.event_count).toBe(2);
    expect(runB.has_graph).toBe(false);
  });

  it('tolerates the root itself being a single run', () => {
    writeRun('', events(3));
    const runs = discoverRuns(tempDir);
    expect(runs).toHaveLength(1);
    expect(runs[0]!.run_id).toBe(path.basename(tempDir));
    expect(runs[0]!.event_count).toBe(3);
  });

  it('counts only parseable events in the summary', () => {
    writeRun('run-a', ['{"event_id":"ok","event_type":"x"}', 'not json', '', '[1,2]']);
    const summary = summarizeRun(tempDir, 'run-a');
    expect(summary!.event_count).toBe(1);
  });

  it('counts artifacts from the kernel index or the directory', () => {
    writeRun('run-a', events(1));
    const artifactsDir = path.join(tempDir, 'run-a', 'artifacts');
    fs.mkdirSync(artifactsDir);
    fs.writeFileSync(path.join(artifactsDir, 'a.txt'), 'hello');
    fs.writeFileSync(path.join(artifactsDir, 'b.bin'), 'x');
    fs.writeFileSync(path.join(artifactsDir, '.stash.tmp'), 'scratch');
    expect(summarizeRun(tempDir, 'run-a')!.artifact_count).toBe(2);

    // With an array index present, the index is authoritative.
    fs.writeFileSync(
      path.join(artifactsDir, 'artifacts.json'),
      JSON.stringify([{ id: 'a.txt' }, { id: 'b.bin' }, { id: 'c.txt' }]),
    );
    expect(summarizeRun(tempDir, 'run-a')!.artifact_count).toBe(3);
  });

  it('counts artifacts from the kernel dict index (keyed by artifact id)', () => {
    writeRun('run-a', events(1));
    const artifactsDir = path.join(tempDir, 'run-a', 'artifacts');
    fs.mkdirSync(artifactsDir);
    fs.writeFileSync(path.join(artifactsDir, 'x.txt'), 'x');
    fs.writeFileSync(
      path.join(artifactsDir, 'artifacts.json'),
      JSON.stringify({ 'a.txt': { artifact_id: 'a.txt' }, 'b.txt': { artifact_id: 'b.txt' } }),
    );
    expect(summarizeRun(tempDir, 'run-a')!.artifact_count).toBe(2);
  });

  it('reports last_modified as ISO from the newest state file', () => {
    writeRun('run-a', events(1), true);
    const dbPath = path.join(tempDir, 'run-a', 'graph.db');
    const future = new Date(Date.now() + 60_000);
    fs.utimesSync(dbPath, future, future);
    const summary = summarizeRun(tempDir, 'run-a')!;
    // Filesystem clocks may round sub-millisecond precision.
    expect(Math.abs(new Date(summary.last_modified).getTime() - future.getTime())).toBeLessThanOrEqual(2);
  });

  it('does not discover an empty root', () => {
    expect(discoverRuns(tempDir)).toEqual([]);
  });
});

describe('resolveRunDir', () => {
  it('resolves an existing run', () => {
    writeRun('run-a', events(1));
    expect(resolveRunDir(tempDir, 'run-a')).toBe(path.join(tempDir, 'run-a'));
  });

  it('returns null for unknown runs', () => {
    writeRun('run-a', events(1));
    expect(resolveRunDir(tempDir, 'nope')).toBeNull();
  });

  it('returns null for unsafe ids (traversal, absolute paths)', () => {
    writeRun('run-a', events(1));
    expect(resolveRunDir(tempDir, '..')).toBeNull();
    expect(resolveRunDir(tempDir, '../run-a')).toBeNull();
    expect(resolveRunDir(tempDir, '/etc')).toBeNull();
    expect(resolveRunDir(tempDir, 'run-a/..')).toBeNull();
    expect(resolveRunDir(tempDir, '')).toBeNull();
  });

  it('returns null for non-run directories', () => {
    fs.mkdirSync(path.join(tempDir, 'empty-dir'));
    expect(resolveRunDir(tempDir, 'empty-dir')).toBeNull();
  });

  it('resolves the root-as-run id to the root directory itself', () => {
    writeRun('', events(1));
    const rootId = path.basename(tempDir);
    const resolved = lookupRun(tempDir, rootId);
    expect(resolved).not.toBeNull();
    expect(resolved!.dir).toBe(tempDir);
    expect(resolved!.summary.run_id).toBe(rootId);
    expect(resolveRunDir(tempDir, rootId)).toBe(tempDir);
  });

  it('lets a subdirectory shadow the root when both share the basename', () => {
    writeRun('', events(1)); // root qualifies
    writeRun(path.basename(tempDir), events(2)); // subdir with same name
    const resolved = lookupRun(tempDir, path.basename(tempDir));
    expect(resolved!.dir).toBe(path.join(tempDir, path.basename(tempDir)));
    expect(resolved!.summary.event_count).toBe(2);
  });
});
