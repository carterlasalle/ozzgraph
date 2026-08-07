import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { readEventsFile } from '../src/eventsLog.js';

let tempDir: string;

beforeEach(() => {
  tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'ozzgraph-events-'));
});

afterEach(() => {
  fs.rmSync(tempDir, { recursive: true, force: true });
});

function logPath(content: string): string {
  const file = path.join(tempDir, 'actions.jsonl');
  fs.writeFileSync(file, content);
  return file;
}

describe('readEventsFile', () => {
  it('parses JSON object lines in order', () => {
    const file = readEventsFile(
      logPath(
        [
          JSON.stringify({ event_id: 'a', event_type: 'x' }),
          JSON.stringify({ event_id: 'b', event_type: 'y' }),
        ].join('\n') + '\n',
      ),
    );
    expect(file.eventCount).toBe(2);
    expect(file.skipped).toBe(0);
    expect(file.events.map((event) => event['event_id'])).toEqual(['a', 'b']);
  });

  it('skips malformed lines gracefully', () => {
    const file = readEventsFile(
      logPath(
        [
          JSON.stringify({ event_id: 'a' }),
          'this is not json',
          '',
          '[1,2,3]',
          '"a string"',
          JSON.stringify({ event_id: 'b' }),
        ].join('\n') + '\n',
      ),
    );
    expect(file.eventCount).toBe(2);
    expect(file.skipped).toBe(3);
    expect(file.events.map((event) => event['event_id'])).toEqual(['a', 'b']);
  });

  it('returns an empty result for a missing file', () => {
    const file = readEventsFile(path.join(tempDir, 'nope.jsonl'));
    expect(file.eventCount).toBe(0);
    expect(file.skipped).toBe(0);
    expect(file.events).toEqual([]);
  });
});
