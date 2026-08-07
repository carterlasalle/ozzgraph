/**
 * Strict validation of user-supplied path segments (run ids, artifact
 * ids). These values are untrusted input that is concatenated into
 * filesystem paths, so anything that could escape the run tree —
 * directory separators, ``..``, absolute paths, NUL bytes, control
 * characters — is rejected. Identifiers are single path segments only.
 */

const MAX_IDENTIFIER_LENGTH = 255;

/**
 * True when ``value`` is safe to use as a single path segment: non-empty,
 * at most 255 chars, no ``/`` ``\\`` NUL or control characters, and not
 * ``.`` or ``..``.
 */
export function isSafeIdentifier(value: string): boolean {
  if (value.length === 0 || value.length > MAX_IDENTIFIER_LENGTH) {
    return false;
  }
  if (value === '.' || value === '..') {
    return false;
  }
  for (let i = 0; i < value.length; i++) {
    const code = value.charCodeAt(i);
    if (
      code === 0x2f || // '/'
      code === 0x5c || // '\\'
      code === 0x00 || // NUL
      code < 0x20
    ) {
      return false;
    }
  }
  return true;
}
