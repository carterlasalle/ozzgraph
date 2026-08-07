/**
 * Typed API error carrying an HTTP status and a stable machine-readable
 * code. Every error response has the shape
 * ``{"error": {"code": string, "message": string}}`` and never leaks
 * stack traces (server code logs details to stderr instead).
 */

export type ErrorCode =
  | 'invalid_run_id'
  | 'invalid_artifact_id'
  | 'run_not_found'
  | 'artifact_not_found'
  | 'graph_not_found'
  | 'graph_read_failed'
  | 'invalid_json_body'
  | 'replay_malformed_event'
  | 'payload_too_large'
  | 'method_not_allowed'
  | 'route_not_found'
  | 'internal_error';

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: ErrorCode,
    message: string,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}
