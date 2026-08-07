/**
 * Lenient reader for the kernel's append-only event log
 * (``actions.jsonl``): one JSON object per line. For the events API the
 * dashboard skips malformed lines gracefully (they are counted, never
 * fatal); strict, kernel-faithful validation lives in ``replay.ts``.
 */

import fs from 'node:fs';

/** The parsed event log. */
export interface EventsFile {
  /** Every parseable JSON-object line, in file order. */
  events: Record<string, unknown>[];
  /** Number of events in {@link events}. */
  eventCount: number;
  /** Number of lines that were not valid JSON objects (incl. blanks). */
  skipped: number;
}

/**
 * Read and parse an actions.jsonl file. A missing file yields an empty
 * result; malformed lines are skipped and counted in ``skipped``.
 */
export function readEventsFile(eventsPath: string): EventsFile {
  const events: Record<string, unknown>[] = [];
  let skipped = 0;
  let raw: string;
  try {
    raw = fs.readFileSync(eventsPath, 'utf8');
  } catch {
    return { events, eventCount: 0, skipped: 0 };
  }
  for (const line of raw.split(/\r?\n/)) {
    if (line.trim().length === 0) {
      // Blank lines (e.g. a trailing newline) are not events but also not
      // malformed content; skip silently.
      continue;
    }
    try {
      const parsed: unknown = JSON.parse(line);
      if (parsed !== null && typeof parsed === 'object' && !Array.isArray(parsed)) {
        events.push(parsed as Record<string, unknown>);
      } else {
        skipped++;
      }
    } catch {
      skipped++;
    }
  }
  return { events, eventCount: events.length, skipped };
}
