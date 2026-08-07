import { describe, expect, it } from 'vitest';

import { normalizeUtcTimestamp } from '../src/timestamps.js';

describe('normalizeUtcTimestamp (Python fromisoformat + astimezone(UTC) + isoformat)', () => {
  it('converts Z timestamps to +00:00', () => {
    expect(normalizeUtcTimestamp('2026-08-06T15:15:00Z')).toBe(
      '2026-08-06T15:15:00+00:00',
    );
    expect(normalizeUtcTimestamp('2026-08-06T15:15:00.123456Z')).toBe(
      '2026-08-06T15:15:00.123456+00:00',
    );
  });

  it('normalizes +00:00 to +00:00 (identity)', () => {
    expect(normalizeUtcTimestamp('2026-08-06T15:15:00+00:00')).toBe(
      '2026-08-06T15:15:00+00:00',
    );
  });

  it('pads fractional seconds to exactly 6 digits like Python', () => {
    expect(normalizeUtcTimestamp('2026-08-06T15:15:00.123Z')).toBe(
      '2026-08-06T15:15:00.123000+00:00',
    );
    expect(normalizeUtcTimestamp('2026-08-06T15:15:00.123456789Z')).toBe(
      '2026-08-06T15:15:00.123456+00:00',
    );
  });

  it('drops all-zero fractional seconds', () => {
    expect(normalizeUtcTimestamp('2026-08-06T15:15:00.000000Z')).toBe(
      '2026-08-06T15:15:00+00:00',
    );
    expect(normalizeUtcTimestamp('2026-08-06T15:15:00.0Z')).toBe(
      '2026-08-06T15:15:00+00:00',
    );
  });

  it('converts non-UTC offsets to UTC', () => {
    expect(normalizeUtcTimestamp('2026-08-06T15:15:00+05:30')).toBe(
      '2026-08-06T09:45:00+00:00',
    );
    expect(normalizeUtcTimestamp('2026-08-06T15:15:00.123456-02:00')).toBe(
      '2026-08-06T17:15:00.123456+00:00',
    );
  });

  it('accepts a space separator like Python fromisoformat', () => {
    expect(normalizeUtcTimestamp('2026-08-06 15:15:00Z')).toBe(
      '2026-08-06T15:15:00+00:00',
    );
  });

  it('rejects naive timestamps (no offset designator)', () => {
    expect(normalizeUtcTimestamp('2026-08-06T15:15:00')).toBeNull();
    expect(normalizeUtcTimestamp('2026-08-06T15:15:00.123456')).toBeNull();
  });

  it('rejects invalid dates, times, and offsets', () => {
    expect(normalizeUtcTimestamp('2026-13-01T00:00:00Z')).toBeNull();
    expect(normalizeUtcTimestamp('2026-02-30T00:00:00Z')).toBeNull();
    expect(normalizeUtcTimestamp('2026-08-06T25:00:00Z')).toBeNull();
    expect(normalizeUtcTimestamp('2026-08-06T00:60:00Z')).toBeNull();
    expect(normalizeUtcTimestamp('2026-08-06T00:00:61Z')).toBeNull();
    expect(normalizeUtcTimestamp('2026-08-06T00:00:00+25:00')).toBeNull();
    expect(normalizeUtcTimestamp('not a timestamp')).toBeNull();
    expect(normalizeUtcTimestamp('')).toBeNull();
  });
});
