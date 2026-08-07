/**
 * Run discovery over a runs root directory.
 *
 * Layout model: the runs root holds one directory per run; a directory
 * qualifies as a run when it directly contains ``actions.jsonl`` and/or
 * ``graph.db`` (the kernel's per-run state files). The root itself is
 * also tolerated as a single run when it directly contains either file
 * (the common single-run layout where the kernel's ``state`` dir is the
 * runs root), in which case its run id is the root directory's basename.
 *
 * Hidden directories (leading ``.``) and symlinked directories are never
 * treated as runs. ``run_id`` values are single path segments; lookups
 * validate them with the path guard so ``..`` and absolute paths can
 * never escape the runs root.
 */

import fs from 'node:fs';
import path from 'node:path';

import { isSafeIdentifier } from './pathguard.js';

/** One discovered run. */
export interface RunSummary {
  run_id: string;
  /** Number of parseable events in ``actions.jsonl`` (0 when absent). */
  event_count: number;
  /** Number of artifacts in the run's ``artifacts`` dir. */
  artifact_count: number;
  /** ISO-8601 mtime of the newest run state file. */
  last_modified: string;
  has_graph: boolean;
  has_events: boolean;
}

/** A resolved run: its directory plus its summary. */
export interface ResolvedRun {
  dir: string;
  summary: RunSummary;
}

/** The kernel's default artifact store file inside the artifacts dir. */
const ARTIFACT_INDEX_FILE = 'artifacts.json';

/**
 * List every discovered run under ``runsDir``, sorted by run id
 * (Unicode code point order — deterministic).
 */
export function discoverRuns(runsDir: string): RunSummary[] {
  const summaries: RunSummary[] = [];

  const entries = safeReaddir(runsDir);
  for (const entry of entries) {
    if (entry.name.startsWith('.') || !entry.isDirectory()) {
      continue;
    }
    const summary = summarizeRun(runsDir, entry.name);
    if (summary !== null) {
      summaries.push(summary);
    }
  }

  // Tolerate the root itself being a single run.
  const rootSummary = summarizeRun(runsDir, '');
  if (rootSummary !== null) {
    const rootId = rootSummary.run_id;
    if (!summaries.some((summary) => summary.run_id === rootId)) {
      summaries.push(rootSummary);
    }
  }

  summaries.sort((a, b) => (a.run_id < b.run_id ? -1 : a.run_id > b.run_id ? 1 : 0));
  return summaries;
}

/**
 * Resolve a validated run id to its directory and summary, or ``null``
 * when the id is unsafe or does not name a run. A subdirectory run wins
 * over the root-as-run case; when the root itself is a single run its
 * id is the runs-root basename (matching {@link discoverRuns}).
 */
export function lookupRun(runsDir: string, runId: string): ResolvedRun | null {
  if (!isSafeIdentifier(runId)) {
    return null;
  }
  const subDir = path.join(runsDir, runId);
  if (isRunDir(subDir)) {
    const summary = summarizeRun(runsDir, runId);
    if (summary !== null) {
      return { dir: subDir, summary };
    }
  }
  // Root-as-run fallback: the root's own run id is its basename.
  if (runId === path.basename(runsDir)) {
    const summary = summarizeRun(runsDir, '');
    if (summary !== null) {
      return { dir: runsDir, summary };
    }
  }
  return null;
}

/**
 * Resolve a validated run id to its absolute directory path, or ``null``
 * when the id is unsafe or does not name a run under ``runsDir``.
 */
export function resolveRunDir(runsDir: string, runId: string): string | null {
  return lookupRun(runsDir, runId)?.dir ?? null;
}

/** True when ``dir`` is a directory that holds run state markers. */
function isRunDir(dir: string): boolean {
  let stat: fs.Stats;
  try {
    stat = fs.statSync(dir);
  } catch {
    return false;
  }
  if (!stat.isDirectory()) {
    return false;
  }
  return fs.existsSync(path.join(dir, 'actions.jsonl')) ||
    fs.existsSync(path.join(dir, 'graph.db'));
}

/**
 * Build the summary for one run directory, or ``null`` when it is not a
 * run. ``runId`` is the directory name; ``''`` means the root itself.
 */
export function summarizeRun(runsDir: string, runId: string): RunSummary | null {
  const dir = runId === '' ? runsDir : path.join(runsDir, runId);
  const eventsPath = path.join(dir, 'actions.jsonl');
  const graphPath = path.join(dir, 'graph.db');

  let eventsStat: fs.Stats | null = null;
  let graphStat: fs.Stats | null = null;
  try {
    eventsStat = fs.statSync(eventsPath);
  } catch {
    // No events file; handled by the null check below.
  }
  try {
    graphStat = fs.statSync(graphPath);
  } catch {
    // No graph file; handled by the null check below.
  }

  const hasEvents = eventsStat !== null && eventsStat.isFile();
  const hasGraph = graphStat !== null && graphStat.isFile();
  if (!hasEvents && !hasGraph) {
    return null;
  }

  const lastModifiedMs = Math.max(
    hasEvents ? eventsStat!.mtimeMs : 0,
    hasGraph ? graphStat!.mtimeMs : 0,
  );

  return {
    run_id: runId === '' ? path.basename(runsDir) : runId,
    event_count: hasEvents ? countEvents(eventsPath) : 0,
    artifact_count: countArtifacts(dir),
    last_modified: new Date(lastModifiedMs).toISOString(),
    has_graph: hasGraph,
    has_events: hasEvents,
  };
}

/** Count parseable event lines in an actions.jsonl file. */
function countEvents(eventsPath: string): number {
  let count = 0;
  for (const line of readLines(eventsPath)) {
    if (line.length === 0) {
      continue;
    }
    try {
      const parsed: unknown = JSON.parse(line);
      if (parsed !== null && typeof parsed === 'object' && !Array.isArray(parsed)) {
        count++;
      }
    } catch {
      // Malformed lines are not events; skip gracefully.
    }
  }
  return count;
}

/**
 * Count artifacts in the run's ``artifacts`` dir. When the kernel's
 * ``artifacts.json`` index exists it is authoritative (a dict keyed by
 * artifact id); otherwise count content files, excluding the index and
 * ``.tmp`` scratch files.
 */
function countArtifacts(runDir: string): number {
  const artifactsDir = path.join(runDir, 'artifacts');
  const indexPath = path.join(artifactsDir, ARTIFACT_INDEX_FILE);
  try {
    const indexRaw = fs.readFileSync(indexPath, 'utf8');
    const index: unknown = JSON.parse(indexRaw);
    if (Array.isArray(index)) {
      return index.length;
    }
    if (index !== null && typeof index === 'object') {
      return Object.keys(index).length;
    }
  } catch {
    // Missing or unreadable index: fall through to directory listing.
  }
  let count = 0;
  const entries = safeReaddir(artifactsDir);
  for (const entry of entries) {
    if (entry.isFile() && !entry.name.startsWith('.') && !entry.name.endsWith('.tmp')) {
      count++;
    }
  }
  return count;
}

function safeReaddir(dir: string): fs.Dirent[] {
  try {
    return fs.readdirSync(dir, { withFileTypes: true });
  } catch {
    return [];
  }
}

function readLines(filePath: string): string[] {
  try {
    const raw = fs.readFileSync(filePath, 'utf8');
    return raw.split(/\r?\n/);
  } catch {
    return [];
  }
}
