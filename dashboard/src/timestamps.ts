/**
 * ISO-8601 timestamp normalization mirroring Python's
 * ``datetime.fromisoformat(...).astimezone(UTC).isoformat()``.
 *
 * The kernel stores and hashes timestamps in exactly the form Python's
 * ``datetime.isoformat()`` produces for a UTC datetime:
 * ``YYYY-MM-DDTHH:MM:SS`` with an optional exactly-six-digit fractional
 * part (``.ffffff``, omitted when zero) and a ``+00:00`` suffix. The
 * event log itself carries ``Z`` or offset suffixes (pydantic writes
 * ``...Z``), so replay must normalize before hashing.
 */

const ISO_RE =
  /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?(Z|([+-])(\d{2}):(\d{2}))$/;

/**
 * Normalize an ISO-8601 timestamp to Python ``isoformat()``-after-UTC
 * form, or ``null`` when the string is not a valid timezone-aware
 * timestamp. A timestamp without a ``Z`` or ``±hh:mm`` offset is naive
 * and rejected, matching the kernel's validator (pydantic rejects naive
 * timestamps).
 */
export function normalizeUtcTimestamp(raw: string): string | null {
  const match = ISO_RE.exec(raw);
  if (match === null) {
    return null;
  }
  const [, y, mo, d, h, mi, s, fracRaw, offsetMarker, offsetSign, offsetH, offsetM] = match;
  const year = Number(y);
  const month = Number(mo);
  const day = Number(d);
  const hour = Number(h);
  const minute = Number(mi);
  const second = Number(s);
  const offsetHours = offsetMarker === 'Z' ? 0 : Number(offsetH);
  const offsetMins = offsetMarker === 'Z' ? 0 : Number(offsetM);
  if (offsetHours > 23 || offsetMins > 59) {
    return null;
  }
  const offsetMinutes =
    offsetMarker === 'Z'
      ? 0
      : (offsetSign === '-' ? -1 : 1) * (offsetHours * 60 + offsetMins);

  // Fractional seconds: truncate to microseconds (6 digits), drop when zero.
  let fraction = '';
  if (fracRaw !== undefined && fracRaw.length > 0) {
    fraction = fracRaw.padEnd(6, '0').slice(0, 6);
    if (/^0+$/.test(fraction)) {
      fraction = '';
    }
  }

  // Validate the calendar date by round-tripping through UTC parts.
  const date = new Date(0);
  date.setUTCFullYear(year, month - 1, day);
  date.setUTCHours(hour, minute, second, 0);
  if (
    date.getUTCFullYear() !== year ||
    date.getUTCMonth() !== month - 1 ||
    date.getUTCDate() !== day ||
    date.getUTCHours() !== hour ||
    date.getUTCMinutes() !== minute ||
    date.getUTCSeconds() !== second
  ) {
    return null;
  }

  const epochMs = date.getTime() - offsetMinutes * 60_000;
  const utc = new Date(epochMs);
  const pad = (value: number, width: number): string =>
    String(value).padStart(width, '0');
  const base = `${pad(utc.getUTCFullYear(), 4)}-${pad(utc.getUTCMonth() + 1, 2)}-${pad(utc.getUTCDate(), 2)}T${pad(utc.getUTCHours(), 2)}:${pad(utc.getUTCMinutes(), 2)}:${pad(utc.getUTCSeconds(), 2)}`;
  return `${base}${fraction === '' ? '' : `.${fraction}`}+00:00`;
}
