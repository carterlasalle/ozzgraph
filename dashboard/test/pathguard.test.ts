import { describe, expect, it } from 'vitest';

import { isSafeIdentifier } from '../src/pathguard.js';

describe('isSafeIdentifier', () => {
  it('accepts plain identifiers', () => {
    expect(isSafeIdentifier('run-abc')).toBe(true);
    expect(isSafeIdentifier('action-123')).toBe(true);
    expect(isSafeIdentifier('a_b.c')).toBe(true);
    expect(isSafeIdentifier('sha256hexdeadbeef')).toBe(true);
  });

  it('rejects traversal and absolute paths', () => {
    expect(isSafeIdentifier('..')).toBe(false);
    expect(isSafeIdentifier('../etc')).toBe(false);
    expect(isSafeIdentifier('a/../b')).toBe(false);
    expect(isSafeIdentifier('/etc/passwd')).toBe(false);
    expect(isSafeIdentifier('a/b')).toBe(false);
    expect(isSafeIdentifier('.')).toBe(false);
  });

  it('rejects backslashes, NUL, and control characters', () => {
    expect(isSafeIdentifier('a\\b')).toBe(false);
    expect(isSafeIdentifier('a\u0000b')).toBe(false);
    expect(isSafeIdentifier('a\u0007b')).toBe(false);
    expect(isSafeIdentifier('a\nb')).toBe(false);
  });

  it('rejects empty and overlong identifiers', () => {
    expect(isSafeIdentifier('')).toBe(false);
    expect(isSafeIdentifier('x'.repeat(256))).toBe(false);
    expect(isSafeIdentifier('x'.repeat(255))).toBe(true);
  });
});
