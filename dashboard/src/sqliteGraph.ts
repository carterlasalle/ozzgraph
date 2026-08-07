/**
 * Read-only access to a run's SQLite state graph (``graph.db``).
 *
 * The kernel owns this database (schema in
 * ``src/ozzgraph/state_graph.py``: ``entities(id, type, data,
 * created_at, updated_at[, data_version])`` and ``edges(id, type,
 * src_id, dst_id, data, created_at)``); the dashboard only ever opens it
 * read-only and never issues a write. All queries are parameterized.
 */

import { createHash } from 'node:crypto';
import fs from 'node:fs';
import { DatabaseSync } from 'node:sqlite';

import {
  codePointCompare,
  parseJsonNode,
  serializeNode,
  type JsonNode,
} from './canonicalJson.js';
import { normalizeUtcTimestamp } from './timestamps.js';

/** One ``entities`` row (raw stored text, as the kernel wrote it). */
export interface GraphEntityRow {
  id: string;
  type: string;
  data: string;
  created_at: string;
  updated_at: string;
}

/** One ``edges`` row. */
export interface GraphEdgeRow {
  id: string;
  type: string;
  src_id: string;
  dst_id: string;
  data: string;
  created_at: string;
}

/** The full read of one graph.db. */
export interface GraphRead {
  schema_version: number;
  entities: GraphEntityRow[];
  edges: GraphEdgeRow[];
  entity_count: number;
  edge_count: number;
  /** sha256 over canonical rows, mirroring ``StateGraph.graph_hash``. */
  graph_hash: string;
}

/** Raised when graph.db exists but cannot be read. */
export class GraphReadError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'GraphReadError';
  }
}

/**
 * Kernel schema version used for hashing; mirrors
 * ``SCHEMA_VERSION`` in ``src/ozzgraph/state_graph.py``. A fresh kernel
 * database always migrates to this version, so replay and live-graph
 * hashes share the header line.
 */
export const KERNEL_SCHEMA_VERSION = 2;

/**
 * Read the full graph (entities + edges + schema version + hash), or
 * ``null`` when ``dbPath`` does not exist. The database is opened
 * read-only; any write attempt fails loudly.
 */
export function readGraph(dbPath: string): GraphRead | null {
  if (!fs.existsSync(dbPath)) {
    return null;
  }
  let db: DatabaseSync;
  try {
    db = new DatabaseSync(dbPath, { readOnly: true });
  } catch (error) {
    throw new GraphReadError(
      `could not open ${dbPath} read-only: ${error instanceof Error ? error.message : String(error)}`,
    );
  }
  try {
    db.exec('PRAGMA query_only = ON');
    const entityRows = db
      .prepare('SELECT id, type, data, created_at, updated_at FROM entities ORDER BY id')
      .all() as unknown as GraphEntityRow[];
    const edgeRows = db
      .prepare(
        'SELECT id, type, src_id, dst_id, data, created_at FROM edges ORDER BY id',
      )
      .all() as unknown as GraphEdgeRow[];
    const versionRow = db.prepare('PRAGMA user_version').get() as
      | { user_version: number }
      | undefined;
    const schemaVersion =
      versionRow === undefined ? 0 : Number(versionRow.user_version);
    return {
      schema_version: schemaVersion,
      entities: entityRows,
      edges: edgeRows,
      entity_count: entityRows.length,
      edge_count: edgeRows.length,
      graph_hash: computeGraphHash(schemaVersion, entityRows, edgeRows),
    };
  } catch (error) {
    throw new GraphReadError(
      `failed to read graph ${dbPath}: ${error instanceof Error ? error.message : String(error)}`,
    );
  } finally {
    db.close();
  }
}

/**
 * sha256 over the canonical graph content, byte-identical to the
 * kernel's ``StateGraph.graph_hash``: a ``schema_version=<n>`` header
 * line, one canonical JSON line per entity ordered by id, and one per
 * edge ordered by id. Stored ``data`` text is parsed and re-serialized
 * with Python's canonical JSON rules (identity for kernel-written rows),
 * and timestamps are normalized to Python ``isoformat()`` form.
 */
export function computeGraphHash(
  schemaVersion: number,
  entities: Array<Pick<GraphEntityRow, 'id' | 'type' | 'data' | 'created_at' | 'updated_at'>>,
  edges: Array<Pick<GraphEdgeRow, 'id' | 'type' | 'src_id' | 'dst_id' | 'data' | 'created_at'>>,
): string {
  const digest = createHash('sha256');
  digest.update(`schema_version=${schemaVersion}\n`);

  const sortedEntities = [...entities].sort((a, b) => codePointCompare(a.id, b.id));
  for (const entity of sortedEntities) {
    digest.update(canonicalRow(entity) + '\n');
  }
  const sortedEdges = [...edges].sort((a, b) => codePointCompare(a.id, b.id));
  for (const edge of sortedEdges) {
    digest.update(canonicalRow(edge) + '\n');
  }
  return digest.digest('hex');
}

/** One canonical JSON line for an entity or edge record. */
function canonicalRow(
  record:
    | Pick<GraphEntityRow, 'id' | 'type' | 'data' | 'created_at' | 'updated_at'>
    | Pick<GraphEdgeRow, 'id' | 'type' | 'src_id' | 'dst_id' | 'data' | 'created_at'>,
): string {
  const entries: Array<{ key: string; value: JsonNode }> = [
    { key: 'created_at', value: stringNode(record.created_at) },
    { key: 'data', value: parseStoredData(record.data) },
    { key: 'id', value: stringNode(record.id) },
  ];
  if ('src_id' in record) {
    entries.push(
      { key: 'src_id', value: stringNode(record.src_id) },
      { key: 'dst_id', value: stringNode(record.dst_id) },
    );
  }
  entries.push({ key: 'type', value: stringNode(record.type) });
  if ('updated_at' in record) {
    entries.push({ key: 'updated_at', value: stringNode(record.updated_at) });
  }
  return serializeNode({ kind: 'object', entries });
}

/** Normalize a stored timestamp; falls back to the raw text when broken. */
function stringNode(raw: string): JsonNode {
  return { kind: 'string', value: normalizeUtcTimestamp(raw) ?? raw };
}

/** Parse stored ``data`` text; broken rows fall back to an empty object. */
function parseStoredData(raw: string): JsonNode {
  try {
    return parseJsonNode(raw);
  } catch {
    return { kind: 'object', entries: [] };
  }
}
