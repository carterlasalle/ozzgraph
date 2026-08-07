/**
 * Deterministic TypeScript replay of a run's event log, mirroring the
 * kernel's ``src/ozzgraph/replay.py`` semantics:
 *
 * - every line is applied in file order;
 * - only the five ``graph.*`` mutation event types are applied; all
 *   other event types are ignored;
 * - a malformed line, a non-object event, a graph event with a
 *   non-object payload, or a payload that fails validation (missing or
 *   mistyped fields, naive timestamp) aborts the replay loudly — replay
 *   never skips a malformed graph event;
 * - deletions and duplicate violations abort like the kernel's typed
 *   errors (``EntityNotFoundError``, ``DuplicateEntityError``, ...).
 *
 * The resulting ``graph_hash`` is byte-identical to the kernel's
 * ``StateGraph.graph_hash`` for kernel-produced logs: header line
 * ``schema_version=<n>``, canonical JSON per entity ordered by id, then
 * per edge ordered by id (see ``canonicalJson.ts``).
 */

import { createHash } from 'node:crypto';
import fs from 'node:fs';

import {
  codePointCompare,
  parseJsonNode,
  serializeNode,
  type JsonNode,
} from './canonicalJson.js';
import { normalizeUtcTimestamp } from './timestamps.js';

/** The five graph-mutation event types replay applies (kernel mirror). */
export const GRAPH_EVENT_TYPES = new Set<string>([
  'graph.entity_created',
  'graph.entity_updated',
  'graph.entity_deleted',
  'graph.edge_created',
  'graph.edge_deleted',
]);

/** Mirrors ``SCHEMA_VERSION`` in ``src/ozzgraph/state_graph.py``. */
export const KERNEL_SCHEMA_VERSION = 2;

/** Result of replaying one event log. */
export interface ReplayResult {
  /** Every parseable event line, including non-graph events. */
  event_count: number;
  /** Final entity count after all mutations. */
  entity_count: number;
  /** Final edge count after all mutations. */
  edge_count: number;
  /** sha256 over canonical rows (kernel-identical for kernel logs). */
  graph_hash: string;
}

/** Raised when a log line or graph-event payload is malformed. */
export class ReplayMalformedError extends Error {
  constructor(
    /** 1-based line number in the log. */
    readonly line: number,
    message: string,
  ) {
    super(message);
    this.name = 'ReplayMalformedError';
  }
}

/** One in-memory entity (data held as canonical JSON text). */
interface EntityState {
  id: string;
  type: string;
  dataText: string;
  createdAt: string;
  updatedAt: string;
}

/** One in-memory edge. */
interface EdgeState {
  id: string;
  type: string;
  srcId: string;
  dstId: string;
  dataText: string;
  createdAt: string;
}

/**
 * Replay ``eventsPath`` (an actions.jsonl file) into an in-memory
 * entity/edge map and return counts plus the canonical graph hash.
 * Throws {@link ReplayMalformedError} with the offending line number on
 * any malformed input; a missing file replays to the empty graph.
 */
export function replayEvents(eventsPath: string): ReplayResult {
  let raw: string;
  try {
    raw = fs.readFileSync(eventsPath, 'utf8');
  } catch {
    raw = '';
  }
  const entities = new Map<string, EntityState>();
  const edges = new Map<string, EdgeState>();
  let eventCount = 0;

  const lines = raw.split(/\r?\n/);
  for (let i = 0; i < lines.length; i++) {
    const lineNumber = i + 1;
    const line = lines[i]!;
    if (line.trim().length === 0) {
      continue;
    }
    let node: JsonNode;
    try {
      node = parseJsonNode(line);
    } catch (error) {
      throw new ReplayMalformedError(
        lineNumber,
        `not valid JSON: ${error instanceof Error ? error.message : String(error)}`,
      );
    }
    if (node.kind !== 'object') {
      throw new ReplayMalformedError(lineNumber, 'event is not a JSON object');
    }
    eventCount++;

    const eventType = stringField(node, 'event_type');
    if (!GRAPH_EVENT_TYPES.has(eventType)) {
      continue;
    }
    const payloadNode = field(node, 'payload');
    if (payloadNode === null || payloadNode.kind !== 'object') {
      throw new ReplayMalformedError(
        lineNumber,
        `${eventType} payload is not an object`,
      );
    }
    applyGraphEvent(lineNumber, eventType, payloadNode, entities, edges);
  }

  const graphHash = computeReplayHash(entities, edges);
  return {
    event_count: eventCount,
    entity_count: entities.size,
    edge_count: edges.size,
    graph_hash: graphHash,
  };
}

/** Apply one validated graph event to the in-memory state. */
function applyGraphEvent(
  lineNumber: number,
  eventType: string,
  payload: JsonNode,
  entities: Map<string, EntityState>,
  edges: Map<string, EdgeState>,
): void {
  switch (eventType) {
    case 'graph.entity_created': {
      const entityId = requiredString(payload, 'entity_id', lineNumber, eventType);
      const entityType = requiredString(payload, 'entity_type', lineNumber, eventType);
      const dataText = dataTextOf(payload, lineNumber, eventType);
      const at = requiredAt(payload, lineNumber, eventType);
      if (entities.has(entityId)) {
        throw new ReplayMalformedError(
          lineNumber,
          `entity '${entityId}' already exists`,
        );
      }
      entities.set(entityId, {
        id: entityId,
        type: entityType,
        dataText,
        createdAt: at,
        updatedAt: at,
      });
      break;
    }
    case 'graph.entity_updated': {
      const entityId = requiredString(payload, 'entity_id', lineNumber, eventType);
      const dataNode = field(payload, 'data');
      if (dataNode === null) {
        throw new ReplayMalformedError(
          lineNumber,
          `${eventType} payload missing required field 'data'`,
        );
      }
      const dataText = canonicalizeNodeOrThrow(dataNode, lineNumber, eventType);
      const at = requiredAt(payload, lineNumber, eventType);
      const existing = entities.get(entityId);
      if (existing === undefined) {
        throw new ReplayMalformedError(
          lineNumber,
          `entity '${entityId}' does not exist`,
        );
      }
      entities.set(entityId, {
        ...existing,
        dataText,
        updatedAt: at,
      });
      break;
    }
    case 'graph.entity_deleted': {
      const entityId = requiredString(payload, 'entity_id', lineNumber, eventType);
      if (!entities.has(entityId)) {
        throw new ReplayMalformedError(
          lineNumber,
          `entity '${entityId}' does not exist`,
        );
      }
      entities.delete(entityId);
      // The kernel cascade-deletes edges touching the entity.
      for (const [edgeId, edge] of edges) {
        if (edge.srcId === entityId || edge.dstId === entityId) {
          edges.delete(edgeId);
        }
      }
      break;
    }
    case 'graph.edge_created': {
      const edgeId = requiredString(payload, 'edge_id', lineNumber, eventType);
      const edgeType = requiredString(payload, 'edge_type', lineNumber, eventType);
      const srcId = requiredString(payload, 'src_id', lineNumber, eventType);
      const dstId = requiredString(payload, 'dst_id', lineNumber, eventType);
      const dataText = dataTextOf(payload, lineNumber, eventType);
      const at = requiredAt(payload, lineNumber, eventType);
      if (edges.has(edgeId)) {
        throw new ReplayMalformedError(lineNumber, `edge id '${edgeId}' already exists`);
      }
      if (!entities.has(srcId)) {
        throw new ReplayMalformedError(
          lineNumber,
          `source entity '${srcId}' does not exist`,
        );
      }
      if (!entities.has(dstId)) {
        throw new ReplayMalformedError(
          lineNumber,
          `destination entity '${dstId}' does not exist`,
        );
      }
      for (const edge of edges.values()) {
        if (edge.type === edgeType && edge.srcId === srcId && edge.dstId === dstId) {
          throw new ReplayMalformedError(
            lineNumber,
            `edge '${edgeType}' '${srcId}'->'${dstId}' already exists`,
          );
        }
      }
      edges.set(edgeId, {
        id: edgeId,
        type: edgeType,
        srcId,
        dstId,
        dataText,
        createdAt: at,
      });
      break;
    }
    case 'graph.edge_deleted': {
      const edgeId = requiredString(payload, 'edge_id', lineNumber, eventType);
      if (!edges.has(edgeId)) {
        throw new ReplayMalformedError(lineNumber, `edge '${edgeId}' does not exist`);
      }
      edges.delete(edgeId);
      break;
    }
  }
}

/** sha256 over canonical rows, mirroring ``StateGraph.graph_hash``. */
function computeReplayHash(
  entities: Map<string, EntityState>,
  edges: Map<string, EdgeState>,
): string {
  const digest = createHash('sha256');
  digest.update(`schema_version=${KERNEL_SCHEMA_VERSION}\n`);
  const entityLines = [...entities.values()]
    .sort((a, b) => codePointCompare(a.id, b.id))
    .map((entity) => {
      const node: JsonNode = {
        kind: 'object',
        entries: [
          { key: 'created_at', value: { kind: 'string', value: entity.createdAt } },
          { key: 'data', value: parseJsonNode(entity.dataText) },
          { key: 'id', value: { kind: 'string', value: entity.id } },
          { key: 'type', value: { kind: 'string', value: entity.type } },
          { key: 'updated_at', value: { kind: 'string', value: entity.updatedAt } },
        ],
      };
      return serializeNode(node);
    });
  for (const line of entityLines) {
    digest.update(line + '\n');
  }
  const edgeLines = [...edges.values()]
    .sort((a, b) => codePointCompare(a.id, b.id))
    .map((edge) => {
      const node: JsonNode = {
        kind: 'object',
        entries: [
          { key: 'created_at', value: { kind: 'string', value: edge.createdAt } },
          { key: 'data', value: parseJsonNode(edge.dataText) },
          { key: 'dst_id', value: { kind: 'string', value: edge.dstId } },
          { key: 'id', value: { kind: 'string', value: edge.id } },
          { key: 'src_id', value: { kind: 'string', value: edge.srcId } },
          { key: 'type', value: { kind: 'string', value: edge.type } },
        ],
      };
      return serializeNode(node);
    });
  for (const line of edgeLines) {
    digest.update(line + '\n');
  }
  return digest.digest('hex');
}

/** Look up one field of an object node. */
function field(node: JsonNode, key: string): JsonNode | null {
  if (node.kind !== 'object') {
    return null;
  }
  for (const entry of node.entries) {
    if (entry.key === key) {
      return entry.value;
    }
  }
  return null;
}

/** Read a string field; empty string is allowed (kernel accepts it). */
function stringField(node: JsonNode, key: string): string {
  const value = field(node, key);
  if (value === null || value.kind !== 'string') {
    return '';
  }
  return value.value;
}

/** Read a required string field or abort the replay. */
function requiredString(
  payload: JsonNode,
  key: string,
  lineNumber: number,
  eventType: string,
): string {
  const value = stringField(payload, key);
  if (value === '' && field(payload, key) === null) {
    throw new ReplayMalformedError(
      lineNumber,
      `${eventType} payload missing required field '${key}'`,
    );
  }
  return value;
}

/** Canonical JSON text of the payload's ``data`` field (default ``{}``). */
function dataTextOf(payload: JsonNode, lineNumber: number, eventType: string): string {
  const dataNode = field(payload, 'data');
  if (dataNode === null) {
    return '{}';
  }
  return canonicalizeNodeOrThrow(dataNode, lineNumber, eventType);
}

/** Canonicalize any JSON node, aborting on non-object data. */
function canonicalizeNodeOrThrow(
  node: JsonNode,
  lineNumber: number,
  eventType: string,
): string {
  if (node.kind !== 'object') {
    throw new ReplayMalformedError(
      lineNumber,
      `${eventType} payload 'data' is not an object`,
    );
  }
  return serializeNode(node);
}

/** Normalized ``at`` timestamp, or abort when missing/invalid. */
function requiredAt(payload: JsonNode, lineNumber: number, eventType: string): string {
  const value = field(payload, 'at');
  if (value === null || value.kind !== 'string') {
    throw new ReplayMalformedError(
      lineNumber,
      `${eventType} payload missing required field 'at'`,
    );
  }
  const normalized = normalizeUtcTimestamp(value.value);
  if (normalized === null) {
    throw new ReplayMalformedError(
      lineNumber,
      `${eventType} payload 'at' is not a valid timezone-aware timestamp`,
    );
  }
  return normalized;
}
