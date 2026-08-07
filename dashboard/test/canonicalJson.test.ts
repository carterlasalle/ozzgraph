import { describe, expect, it } from 'vitest';

import {
  canonicalizeJsonText,
  codePointCompare,
  escapeString,
  parseJsonNode,
  serializeNode,
} from '../src/canonicalJson.js';

describe('canonicalizeJsonText (Python json.dumps sort_keys + ensure_ascii)', () => {
  it('sorts keys at every nesting level', () => {
    expect(canonicalizeJsonText('{"b":1,"a":{"d":4,"c":3}}')).toBe(
      '{"a":{"c":3,"d":4},"b":1}',
    );
  });

  it('uses compact separators', () => {
    expect(canonicalizeJsonText('{"a": [1, 2, 3]}')).toBe('{"a":[1,2,3]}');
  });

  it('preserves raw number literals like Python reprs', () => {
    expect(canonicalizeJsonText('{"x":1.0}')).toBe('{"x":1.0}');
    expect(canonicalizeJsonText('{"x":1}')).toBe('{"x":1}');
    expect(canonicalizeJsonText('{"x":1e-05}')).toBe('{"x":1e-05}');
    expect(canonicalizeJsonText('{"x":1E+05}')).toBe('{"x":1E+05}');
    expect(canonicalizeJsonText('{"x":123456789012345678901234567890}')).toBe(
      '{"x":123456789012345678901234567890}',
    );
  });

  it('escapes non-ASCII as \\uXXXX like ensure_ascii=True', () => {
    expect(canonicalizeJsonText('{"note":"café"}')).toBe('{"note":"caf\\u00e9"}');
  });

  it('escapes control characters with short forms and \\uXXXX', () => {
    expect(escapeString('a\nb\tc\r\x01')).toBe('"a\\nb\\tc\\r\\u0001"');
    expect(canonicalizeJsonText('{"x":"\\u0001"}')).toBe('{"x":"\\u0001"}');
  });

  it('escapes astral characters as surrogate pairs (Python style)', () => {
    expect(escapeString('😀')).toBe('"\\ud83d\\ude00"');
  });

  it('keeps printable ASCII and quote/backslash escapes', () => {
    expect(escapeString('say "hi" \\ ok')).toBe('"say \\"hi\\" \\\\ ok"');
  });

  it('is idempotent', () => {
    const once = canonicalizeJsonText('{"b":{"d":1,"c":2},"a":1.0}');
    expect(canonicalizeJsonText(once)).toBe(once);
  });

  it('handles empty containers and nulls', () => {
    expect(canonicalizeJsonText('{}')).toBe('{}');
    expect(canonicalizeJsonText('[]')).toBe('[]');
    expect(canonicalizeJsonText('{"a":null,"b":[true,false]}')).toBe(
      '{"a":null,"b":[true,false]}',
    );
  });

  it('rejects malformed JSON loudly', () => {
    expect(() => canonicalizeJsonText('{"a":')).toThrow();
    expect(() => canonicalizeJsonText('tru')).toThrow();
    expect(() => canonicalizeJsonText('{"a":01}')).toThrow();
    expect(() => canonicalizeJsonText('{"a":1.}')).toThrow();
    expect(() => canonicalizeJsonText('{"a" 1}')).toThrow();
    expect(() => canonicalizeJsonText('"unterminated')).toThrow();
    expect(() => canonicalizeJsonText('{"a":"\\x"}')).toThrow();
  });
});

describe('codePointCompare', () => {
  it('orders by Unicode code point like Python sorted()', () => {
    const values = ['z', '😀', 'a', 'é'];
    values.sort(codePointCompare);
    expect(values).toEqual(['a', 'z', 'é', '😀']);
  });

  it('compares prefixes and code-point lengths', () => {
    expect(codePointCompare('ab', 'abc')).toBeLessThan(0);
    expect(codePointCompare('abc', 'ab')).toBeGreaterThan(0);
    expect(codePointCompare('same', 'same')).toBe(0);
  });
});

describe('parseJsonNode round-trip', () => {
  it('preserves number literals through parse + serialize', () => {
    const node = parseJsonNode('{"a":1.0,"b":[1e-05,-2.5e+3],"c":"x"}');
    expect(serializeNode(node)).toBe('{"a":1.0,"b":[1e-05,-2.5e+3],"c":"x"}');
  });
});
