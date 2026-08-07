/**
 * Run metrics derived from the event log. All counts are deterministic
 * functions of the log content; malformed lines are skipped gracefully
 * (never fatal) and reported in ``skipped``.
 */

import { readEventsFile } from './eventsLog.js';

/** Metrics derived from one run's event log. */
export interface RunMetrics {
  run_id: string;
  /** Parseable events in the log (including non-graph events). */
  total_events: number;
  /** Lines that were not valid JSON objects (incl. blanks). */
  skipped: number;
  /** Count per ``event_type``. */
  event_type_counts: Record<string, number>;
  /** Count per ``producer``. */
  producer_counts: Record<string, number>;
  /** ``graph.entity_created`` events whose entity type is ``action``. */
  action_count: number;
  /** ``graph.entity_created`` events whose entity type is ``model_call``. */
  model_call_count: number;
  /** Oldest valid event timestamp (ISO-8601) or null. */
  first_event_at: string | null;
  /** Newest valid event timestamp (ISO-8601) or null. */
  last_event_at: string | null;
  /** Wall-clock span between first and last event in ms, or null. */
  duration_ms: number | null;
}

/** Derive metrics from ``eventsPath`` (an actions.jsonl file). */
export function deriveMetrics(runId: string, eventsPath: string): RunMetrics {
  const { events, eventCount, skipped } = readEventsFile(eventsPath);

  const eventTypeCounts: Record<string, number> = {};
  const producerCounts: Record<string, number> = {};
  let actionCount = 0;
  let modelCallCount = 0;
  let firstMs: number | null = null;
  let lastMs: number | null = null;

  for (const event of events) {
    const eventType = typeof event['event_type'] === 'string' ? event['event_type'] : '';
    const producer = typeof event['producer'] === 'string' ? event['producer'] : '';
    eventTypeCounts[eventType] = (eventTypeCounts[eventType] ?? 0) + 1;
    if (producer.length > 0) {
      producerCounts[producer] = (producerCounts[producer] ?? 0) + 1;
    }
    if (eventType === 'graph.entity_created') {
      const payload = event['payload'];
      const entityType =
        payload !== null &&
        typeof payload === 'object' &&
        !Array.isArray(payload) &&
        typeof (payload as Record<string, unknown>)['entity_type'] === 'string'
          ? ((payload as Record<string, unknown>)['entity_type'] as string)
          : '';
      if (entityType === 'action') {
        actionCount++;
      } else if (entityType === 'model_call') {
        modelCallCount++;
      }
    }
    const timestamp = typeof event['timestamp'] === 'string' ? event['timestamp'] : '';
    const ms = Date.parse(timestamp);
    if (Number.isFinite(ms)) {
      firstMs = firstMs === null ? ms : Math.min(firstMs, ms);
      lastMs = lastMs === null ? ms : Math.max(lastMs, ms);
    }
  }

  return {
    run_id: runId,
    total_events: eventCount,
    skipped,
    event_type_counts: eventTypeCounts,
    producer_counts: producerCounts,
    action_count: actionCount,
    model_call_count: modelCallCount,
    first_event_at: firstMs === null ? null : new Date(firstMs).toISOString(),
    last_event_at: lastMs === null ? null : new Date(lastMs).toISOString(),
    duration_ms: firstMs === null || lastMs === null ? null : lastMs - firstMs,
  };
}
