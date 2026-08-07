/**
 * Token-preserving JSON canonicalization mirroring Python's
 * ``json.dumps(..., sort_keys=True, separators=(",", ":"))`` with the
 * default ``ensure_ascii=True``.
 *
 * The OzzGraph kernel computes its graph hash over canonical JSON lines
 * produced by exactly that call (see ``src/ozzgraph/state_graph.py``:
 * ``_dumps`` / ``_canonical_entity_line`` / ``_canonical_edge_line``).
 * To reproduce the same digest byte-for-byte, this module:
 *
 * - parses JSON with a small recursive-descent parser that preserves the
 *   *raw literal text* of numbers (``1.0`` stays ``1.0``, ``1e-05`` stays
 *   ``1e-05``) instead of round-tripping through IEEE-754 doubles, and
 * - serializes strings with Python's ``ensure_ascii`` escape rules
 *   (``"`` ``\\`` control chars and every code unit >= U+007F become
 *   ``\\uXXXX`` / short escapes, astral characters become surrogate
 *   pairs), because the kernel's canonical lines escape non-ASCII.
 *
 * Keys are sorted by Unicode code point, matching Python's ``sorted()``
 * on ``str`` keys.
 */

/** One node of the token-preserving parse tree. */
export type JsonNode =
  | { kind: 'object'; entries: Array<{ key: string; value: JsonNode }> }
  | { kind: 'array'; items: JsonNode[] }
  | { kind: 'string'; value: string }
  | { kind: 'number'; literal: string }
  | { kind: 'true' }
  | { kind: 'false' }
  | { kind: 'null' };

/** Raised when raw JSON text cannot be parsed. */
export class JsonParseError extends Error {
  constructor(
    readonly offset: number,
    message: string,
  ) {
    super(message);
    this.name = 'JsonParseError';
  }
}

/** Compare two strings by Unicode code point (Python ``str`` ordering). */
export function codePointCompare(a: string, b: string): number {
  const pa = Array.from(a);
  const pb = Array.from(b);
  const n = Math.min(pa.length, pb.length);
  for (let i = 0; i < n; i++) {
    const ca = pa[i]!.codePointAt(0)!;
    const cb = pb[i]!.codePointAt(0)!;
    if (ca !== cb) {
      return ca < cb ? -1 : 1;
    }
  }
  return pa.length - pb.length;
}

const NUMBER_RE = /^-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?$/;

/**
 * Parse raw JSON text into a {@link JsonNode} tree, preserving number
 * literals verbatim. Throws {@link JsonParseError} on malformed input.
 */
export function parseJsonNode(raw: string): JsonNode {
  const parser = new Parser(raw);
  const node = parser.parseValue();
  parser.skipWhitespace();
  if (!parser.atEnd()) {
    throw parser.error('unexpected trailing content');
  }
  return node;
}

/** Canonicalize a raw JSON text (must parse; see module docstring). */
export function canonicalizeJsonText(raw: string): string {
  return serializeNode(parseJsonNode(raw));
}

/** Canonicalize an already-parsed {@link JsonNode} tree. */
export function serializeNode(node: JsonNode): string {
  switch (node.kind) {
    case 'object': {
      const entries = [...node.entries].sort((a, b) =>
        codePointCompare(a.key, b.key),
      );
      const body = entries
        .map((entry) => `${escapeString(entry.key)}:${serializeNode(entry.value)}`)
        .join(',');
      return `{${body}}`;
    }
    case 'array':
      return `[${node.items.map(serializeNode).join(',')}]`;
    case 'string':
      return escapeString(node.value);
    case 'number':
      return node.literal;
    case 'true':
      return 'true';
    case 'false':
      return 'false';
    case 'null':
      return 'null';
  }
}

/**
 * Serialize a JS string as a JSON string literal using Python's
 * ``ensure_ascii=True`` rules.
 */
export function escapeString(value: string): string {
  let out = '"';
  for (let i = 0; i < value.length; i++) {
    const code = value.charCodeAt(i);
    const ch = value[i]!;
    switch (ch) {
      case '"':
        out += '\\"';
        break;
      case '\\':
        out += '\\\\';
        break;
      case '\b':
        out += '\\b';
        break;
      case '\f':
        out += '\\f';
        break;
      case '\n':
        out += '\\n';
        break;
      case '\r':
        out += '\\r';
        break;
      case '\t':
        out += '\\t';
        break;
      default:
        if (code < 0x20 || code >= 0x7f) {
          out += `\\u${code.toString(16).padStart(4, '0')}`;
        } else {
          out += ch;
        }
    }
  }
  return `${out}"`;
}

/** Recursive-descent JSON parser preserving raw number literals. */
class Parser {
  private pos = 0;

  constructor(private readonly text: string) {}

  atEnd(): boolean {
    return this.pos >= this.text.length;
  }

  error(message: string): JsonParseError {
    return new JsonParseError(this.pos, `${message} at offset ${this.pos}`);
  }

  skipWhitespace(): void {
    while (!this.atEnd()) {
      const ch = this.text[this.pos]!;
      if (ch === ' ' || ch === '\t' || ch === '\n' || ch === '\r') {
        this.pos++;
      } else {
        break;
      }
    }
  }

  private peek(): string {
    if (this.atEnd()) {
      throw this.error('unexpected end of input');
    }
    return this.text[this.pos]!;
  }

  private consume(expected: string): void {
    if (this.peek() !== expected) {
      throw this.error(`expected '${expected}'`);
    }
    this.pos++;
  }

  parseValue(): JsonNode {
    this.skipWhitespace();
    const ch = this.peek();
    switch (ch) {
      case '{':
        return this.parseObject();
      case '[':
        return this.parseArray();
      case '"':
        return { kind: 'string', value: this.parseString() };
      case 't':
        this.consumeLiteral('true');
        return { kind: 'true' };
      case 'f':
        this.consumeLiteral('false');
        return { kind: 'false' };
      case 'n':
        this.consumeLiteral('null');
        return { kind: 'null' };
      default:
        if (ch === '-' || (ch >= '0' && ch <= '9')) {
          return { kind: 'number', literal: this.parseNumber() };
        }
        throw this.error(`unexpected character '${ch}'`);
    }
  }

  private consumeLiteral(literal: string): void {
    for (const expected of literal) {
      if (this.peek() !== expected) {
        throw this.error(`expected '${literal}'`);
      }
      this.pos++;
    }
  }

  private parseObject(): JsonNode {
    this.consume('{');
    const entries: Array<{ key: string; value: JsonNode }> = [];
    this.skipWhitespace();
    if (this.peek() === '}') {
      this.pos++;
      return { kind: 'object', entries };
    }
    for (;;) {
      this.skipWhitespace();
      if (this.peek() !== '"') {
        throw this.error("expected object key string");
      }
      const key = this.parseString();
      this.skipWhitespace();
      this.consume(':');
      const value = this.parseValue();
      entries.push({ key, value });
      this.skipWhitespace();
      const sep = this.peek();
      if (sep === ',') {
        this.pos++;
        continue;
      }
      if (sep === '}') {
        this.pos++;
        return { kind: 'object', entries };
      }
      throw this.error("expected ',' or '}' in object");
    }
  }

  private parseArray(): JsonNode {
    this.consume('[');
    const items: JsonNode[] = [];
    this.skipWhitespace();
    if (this.peek() === ']') {
      this.pos++;
      return { kind: 'array', items };
    }
    for (;;) {
      items.push(this.parseValue());
      this.skipWhitespace();
      const sep = this.peek();
      if (sep === ',') {
        this.pos++;
        continue;
      }
      if (sep === ']') {
        this.pos++;
        return { kind: 'array', items };
      }
      throw this.error("expected ',' or ']' in array");
    }
  }

  private parseString(): string {
    this.consume('"');
    let out = '';
    for (;;) {
      if (this.atEnd()) {
        throw this.error('unterminated string');
      }
      const ch = this.text[this.pos]!;
      if (ch === '"') {
        this.pos++;
        return out;
      }
      if (ch === '\\') {
        this.pos++;
        if (this.atEnd()) {
          throw this.error('unterminated escape sequence');
        }
        const esc = this.text[this.pos]!;
        this.pos++;
        switch (esc) {
          case '"':
            out += '"';
            break;
          case '\\':
            out += '\\';
            break;
          case '/':
            out += '/';
            break;
          case 'b':
            out += '\b';
            break;
          case 'f':
            out += '\f';
            break;
          case 'n':
            out += '\n';
            break;
          case 'r':
            out += '\r';
            break;
          case 't':
            out += '\t';
            break;
          case 'u': {
            const hex = this.text.slice(this.pos, this.pos + 4);
            if (!/^[0-9a-fA-F]{4}$/.test(hex)) {
              throw this.error('invalid \\u escape');
            }
            out += String.fromCharCode(Number.parseInt(hex, 16));
            this.pos += 4;
            break;
          }
          default:
            throw this.error(`invalid escape '\\${esc}'`);
        }
      } else {
        const code = ch.charCodeAt(0);
        if (code < 0x20) {
          throw this.error('unescaped control character in string');
        }
        out += ch;
        this.pos++;
      }
    }
  }

  private parseNumber(): string {
    const start = this.pos;
    if (this.peek() === '-') {
      this.pos++;
    }
    while (!this.atEnd() && /[0-9.eE+-]/.test(this.text[this.pos]!)) {
      this.pos++;
    }
    const literal = this.text.slice(start, this.pos);
    if (!NUMBER_RE.test(literal)) {
      throw this.error(`invalid number literal '${literal}'`);
    }
    return literal;
  }
}
