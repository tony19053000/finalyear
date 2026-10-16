/**
 * Reads `compute.yaml` in the browser (D177) - a YAML SUBSET, behind one
 * function, until the team decides on a YAML library (question Q-G4-2 in the
 * G4 progress file; swap = replace `parseManifestYaml`'s body).
 *
 * Read like the CLI's loader (PyYAML `safe_load`, YAML 1.1 typing, aliases and
 * duplicate keys refused - common/manifest.py): block mappings and lists
 * (`key: value`, `- item`, `- key: v` items), flow lists/maps (`[a, b]`,
 * `{name: X, as_env: true}`, also across lines), plain / 'single' / "double"
 * quoted scalars, comments, `---`. Plain scalars are typed as YAML 1.1 does:
 * yes/no/on/off/true/false, ~/null, ints (incl. 0x, 0b, leading-0 octal,
 * `_`, `1:30` = 90), floats (`3.10` = 3.1, `1e3` stays a string).
 *
 * Anything outside the subset - block scalars (`|`, `>`), anchors, aliases,
 * tags, `?` (anywhere), multi-line plain scalars, dates, .inf/.nan,
 * non-string keys, Unicode line separators (U+0085/2028/2029) - is refused
 * with `unsupported: true`: the message tells the user to submit with the
 * CLI. It is never guessed. What PyYAML rejects (tabs outside quotes and
 * comments, control characters, stray `]`/`}`/`,`, empty flow entries,
 * `key: - x`) is refused too. White space is YAML's: space and tab only -
 * NBSP, U+3000 etc. are content. The principle (D196): refuse rather than
 * mis-parse; tester r1's 4 898-case differential against the CLI's loader
 * keeps it honest.
 */

export type YamlValue = null | boolean | number | string | YamlValue[] | { [key: string]: YamlValue };

export class ManifestYamlError extends Error {
  override readonly name = "ManifestYamlError";
  readonly line: number | undefined;
  /** True: valid YAML perhaps, but outside what the browser reads. */
  readonly unsupported: boolean;
  constructor(message: string, line?: number, unsupported = false) {
    super(line === undefined ? message : `line ${line}: ${message}`);
    this.line = line;
    this.unsupported = unsupported;
  }
}

function unsupported(what: string, line?: number): ManifestYamlError {
  return new ManifestYamlError(`${what} - the dashboard cannot read this; submit with \`compute run\``, line, true);
}

/* ---------------------------------------------------------- characters */

/** YAML white space is space and tab only (JS `\s`/`trim()` would eat NBSP, U+3000, ...). */
function isWs(ch: string): boolean {
  return ch === " " || ch === "\t";
}

function trimWs(text: string): string {
  return text.replace(/^[ \t]+|[ \t]+$/g, "");
}

function trimWsEnd(text: string): string {
  return text.replace(/[ \t]+$/, "");
}

/** PyYAML's reader refuses anything outside its printable set (control chars, lone surrogates). */
const NON_PRINTABLE = /[^\t\n\r\x20-\x7e\x85\xa0-\ud7ff\ue000-\ufffd\u{10000}-\u{10ffff}]/u;
/** Line breaks to PyYAML, but not to this reader: refused instead of guessed. */
const UNICODE_BREAKS = /[\x85\u2028\u2029]/;

function lineOf(text: string, index: number): number {
  return text.slice(0, index).split(/\r\n|\r|\n/).length;
}

function checkCharacters(text: string): void {
  const bad = NON_PRINTABLE.exec(text);
  if (bad !== null) {
    const code = bad[0].codePointAt(0) ?? 0;
    throw new ManifestYamlError(`control character U+${code.toString(16).toUpperCase().padStart(4, "0")} is not allowed`, lineOf(text, bad.index));
  }
  const brk = UNICODE_BREAKS.exec(text);
  if (brk !== null) throw unsupported("a Unicode line separator (U+0085, U+2028 or U+2029)", lineOf(text, brk.index));
}

interface Line {
  no: number;
  indent: number;
  text: string;
}

/* ------------------------------------------------------------ scalars */

const NULL_RE = /^(?:~|null|Null|NULL|)$/;
const BOOL_RE = /^(?:yes|Yes|YES|no|No|NO|true|True|TRUE|false|False|FALSE|on|On|ON|off|Off|OFF)$/;
const TRUE_RE = /^(?:yes|Yes|YES|true|True|TRUE|on|On|ON)$/;
const INT_RE = /^(?:[-+]?0b[0-1_]+|[-+]?0[0-7_]+|[-+]?(?:0|[1-9][0-9_]*)|[-+]?0x[0-9a-fA-F_]+|[-+]?[1-9][0-9_]*(?::[0-5]?[0-9])+)$/;
const FLOAT_RE =
  /^(?:[-+]?(?:[0-9][0-9_]*)\.[0-9_]*(?:[eE][-+][0-9]+)?|\.[0-9_]+(?:[eE][-+][0-9]+)?|[-+]?[0-9][0-9_]*(?::[0-5]?[0-9])+\.[0-9_]*|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))$/;
const TIMESTAMP_RE =
  /^(?:[0-9]{4}-[0-9]{2}-[0-9]{2}|[0-9]{4}-[0-9]{1,2}-[0-9]{1,2}(?:[Tt]|[ \t]+)[0-9]{1,2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]*)?(?:[ \t]*(?:Z|[-+][0-9]{1,2}(?::[0-9]{2})?))?)$/;

function toInt(text: string, line: number): number {
  let value = text.replace(/_/g, "");
  let sign = 1;
  if (value.startsWith("-")) sign = -1;
  if (value.startsWith("-") || value.startsWith("+")) value = value.slice(1);
  let result: number;
  if (value === "0") result = 0;
  else if (value.startsWith("0b")) result = parseInt(value.slice(2), 2);
  else if (value.startsWith("0x")) result = parseInt(value.slice(2), 16);
  else if (value.startsWith("0")) result = parseInt(value, 8);
  else if (value.includes(":")) {
    result = value
      .split(":")
      .map((d) => parseInt(d, 10))
      .reduce((acc, digit) => acc * 60 + digit, 0);
  } else result = parseInt(value, 10);
  result *= sign;
  if (!Number.isSafeInteger(result)) throw unsupported(`the number ${text} is too large`, line);
  return result;
}

function toFloat(text: string, line: number): number {
  const value = text.replace(/_/g, "").toLowerCase();
  if (value.includes("inf") || value.includes("nan") || value.includes(":")) {
    throw unsupported(`the number ${text}`, line);
  }
  return parseFloat(value);
}

/** A plain scalar typed as PyYAML's implicit resolvers do. */
export function resolvePlain(text: string, line: number): YamlValue {
  if (NULL_RE.test(text)) return null;
  if (BOOL_RE.test(text)) return TRUE_RE.test(text);
  if (INT_RE.test(text)) return toInt(text, line);
  if (FLOAT_RE.test(text)) return toFloat(text, line);
  if (TIMESTAMP_RE.test(text)) throw unsupported(`the date ${text} (quote it to keep it text)`, line);
  if (text === "<<" || text === "=") throw unsupported(`the YAML key "${text}"`, line);
  return text;
}

const ESCAPES: Record<string, string> = {
  "0": "\0",
  a: "\x07",
  b: "\b",
  t: "\t",
  "\t": "\t",
  n: "\n",
  v: "\v",
  f: "\f",
  r: "\r",
  e: "\x1b",
  " ": " ",
  '"': '"',
  "/": "/",
  "\\": "\\",
  N: "\x85",
  _: "\xa0",
  L: "\u2028",
  P: "\u2029",
};
const HEX_LENGTH: Record<string, number> = { x: 2, u: 4, U: 8 };

/** A quoted scalar starting at `text[start]`; returns [value, index after it]. */
function readQuoted(text: string, start: number, line: number): [string, number] {
  const quote = text[start];
  let out = "";
  let i = start + 1;
  while (i < text.length) {
    const ch = text[i] ?? "";
    if (quote === "'") {
      if (ch === "'") {
        if (text[i + 1] === "'") {
          out += "'";
          i += 2;
          continue;
        }
        return [out, i + 1];
      }
      out += ch;
      i += 1;
      continue;
    }
    if (ch === '"') return [out, i + 1];
    if (ch === "\\") {
      const code = text[i + 1] ?? "";
      const simple = ESCAPES[code];
      if (simple !== undefined) {
        out += simple;
        i += 2;
        continue;
      }
      const length = HEX_LENGTH[code];
      if (length !== undefined) {
        const hex = text.slice(i + 2, i + 2 + length);
        if (!/^[0-9a-fA-F]+$/.test(hex) || hex.length !== length) {
          throw new ManifestYamlError(`bad escape \\${code}${hex}`, line);
        }
        const point = parseInt(hex, 16);
        if (point > 0x10ffff || (point >= 0xd800 && point <= 0xdfff)) {
          // String.fromCodePoint would throw a RangeError / make a lone surrogate (T-3).
          throw new ManifestYamlError(`the escape \\${code}${hex} is not a valid character`, line);
        }
        out += String.fromCodePoint(point);
        i += 2 + length;
        continue;
      }
      throw new ManifestYamlError(`unknown escape \\${code}`, line);
    }
    out += ch;
    i += 1;
  }
  throw new ManifestYamlError("a quoted string is not closed on its line", line);
}

/* --------------------------------------------------------------- lines */

/** Strip a comment (`#` at the start or after a space, outside quotes). */
function stripComment(text: string, line: number): string {
  let i = 0;
  let prev = "";
  while (i < text.length) {
    const ch = text[i] ?? "";
    if ((ch === '"' || ch === "'") && /^$|[ \t:[{,-]$/.test(prev) && quoteStarts(text, i)) {
      const [, end] = readQuoted(text, i, line);
      i = end;
      prev = ch;
      continue;
    }
    if (ch === "#" && (i === 0 || isWs(text[i - 1] ?? ""))) return trimWsEnd(text.slice(0, i));
    if (ch === "\t") throw new ManifestYamlError("a tab is only allowed inside quotes or a comment", line);
    if (!isWs(ch)) prev = ch;
    i += 1;
  }
  return trimWsEnd(text);
}

/** A quote opens a quoted scalar only where a token starts. */
function quoteStarts(text: string, i: number): boolean {
  const before = trimWsEnd(text.slice(0, i));
  if (before === "") return true;
  const last = before[before.length - 1] ?? "";
  if (last === "[" || last === "{" || last === ",") return true;
  // `key: "v"` or `- "v"`: the indicator must be followed by a space.
  return (last === ":" || last === "-" || last === "?") && isWs(text[before.length] ?? "");
}

function splitLinesOf(text: string): Line[] {
  const raw = text.replace(/^\uFEFF/, "").split(/\r\n|\r|\n/);
  const lines: Line[] = [];
  raw.forEach((source, index) => {
    const no = index + 1;
    const indentMatch = /^[ \t]*/.exec(source)?.[0] ?? "";
    const content = source.slice(indentMatch.length);
    // PyYAML refuses a blank line holding a tab once content has started (T-17).
    if (content === "" && indentMatch.includes("\t") && lines.length > 0) {
      throw new ManifestYamlError("a tab is only allowed inside quotes or a comment", no);
    }
    if (content === "" || content.startsWith("#")) return;
    if (indentMatch.includes("\t")) throw new ManifestYamlError("tabs cannot indent YAML", no);
    const clean = stripComment(content, no);
    if (clean === "") return;
    if (index === 0 || lines.length === 0) {
      if (clean.startsWith("%")) throw unsupported("YAML directives", no);
    }
    if (clean === "---" || clean.startsWith("--- ")) {
      if (lines.length > 0) throw unsupported("several YAML documents", no);
      if (clean !== "---") throw unsupported("content after ---", no);
      return;
    }
    if (clean === "...") return;
    lines.push({ no, indent: indentMatch.length, text: clean });
  });
  return lines;
}

/* ---------------------------------------------------------------- flow */

class Flow {
  i = 0;
  constructor(
    readonly text: string,
    readonly line: number,
  ) {}

  skip(): void {
    while (this.i < this.text.length && (isWs(this.text[this.i] ?? "") || this.text[this.i] === "\n")) this.i += 1;
  }

  peek(): string {
    return this.text[this.i] ?? "";
  }

  value(): YamlValue {
    this.skip();
    const ch = this.peek();
    if (ch === "[") return this.list();
    if (ch === "{") return this.map();
    if (ch === '"' || ch === "'") {
      const [value, end] = readQuoted(this.text, this.i, this.line);
      this.i = end;
      return value;
    }
    if (ch === "") throw new ManifestYamlError("a flow collection ends too early", this.line);
    checkIndicator(ch, this.line);
    const after = this.text[this.i + 1] ?? "";
    // In a flow collection `:` never starts a plain scalar, and `- x` is no item.
    if (ch === ":" || (ch === "-" && (after === "" || after === "\n" || isWs(after)))) {
      throw new ManifestYamlError(`unexpected ${ch} in a flow collection`, this.line);
    }
    const start = this.i;
    while (this.i < this.text.length) {
      const c = this.peek();
      if (c === "," || c === "]" || c === "}") break;
      if (c === ":" && /^[ \t\n,[\]{}]?$/.test(this.text[this.i + 1] ?? "")) break;
      // PyYAML ends a flow plain scalar here and then fails or reads it differently.
      if (c === "[" || c === "{" || c === "?") throw new ManifestYamlError(`unexpected ${c} inside a flow value`, this.line);
      this.i += 1;
    }
    const raw = this.text.slice(start, this.i).replace(/^[ \t\n]+|[ \t\n]+$/g, "");
    // PyYAML folds a plain scalar split across lines (blank lines -> "\n"); never guessed here.
    if (raw.includes("\n")) throw unsupported("a value split across lines inside [ ] or { }", this.line);
    return resolvePlain(raw, this.line);
  }

  list(): YamlValue[] {
    this.i += 1;
    const out: YamlValue[] = [];
    for (;;) {
      this.skip();
      if (this.peek() === "]") {
        this.i += 1;
        return out;
      }
      if (this.i >= this.text.length) throw new ManifestYamlError("a [ list is not closed", this.line);
      if (this.peek() === ",") throw new ManifestYamlError("an empty entry in a [ list", this.line);
      out.push(this.value());
      this.skip();
      if (this.peek() === ",") this.i += 1;
      else if (this.peek() !== "]") throw new ManifestYamlError("expected , or ] in a list", this.line);
    }
  }

  map(): { [key: string]: YamlValue } {
    this.i += 1;
    const out: { [key: string]: YamlValue } = {};
    for (;;) {
      this.skip();
      if (this.peek() === "}") {
        this.i += 1;
        return out;
      }
      if (this.i >= this.text.length) throw new ManifestYamlError("a { map is not closed", this.line);
      if (this.peek() === ",") throw new ManifestYamlError("an empty entry in a { map", this.line);
      const keyStart = this.i;
      const key = this.value();
      this.skip();
      if (this.peek() === ":" && this.text.slice(keyStart, this.i).includes("\n")) {
        throw new ManifestYamlError("a key and its : must be on one line", this.line);
      }
      let value: YamlValue = null;
      if (this.peek() === ":") {
        this.i += 1;
        this.skip();
        value = this.peek() === "," || this.peek() === "}" ? null : this.value();
      }
      setKey(out, key, value, this.line);
      this.skip();
      if (this.peek() === ",") this.i += 1;
      else if (this.peek() !== "}") throw new ManifestYamlError("expected , or } in a map", this.line);
    }
  }
}

function checkIndicator(ch: string, line: number): void {
  if (ch === "?") throw unsupported("the ? (complex key) indicator - quote the text if it starts with ?", line);
  if (ch === "," || ch === "]" || ch === "}" || ch === "#") throw new ManifestYamlError(`a value cannot start with ${ch}`, line);
  if (ch === "&") throw unsupported("YAML anchors (&name)", line);
  if (ch === "*") throw new ManifestYamlError("YAML aliases (*name) are not allowed", line);
  if (ch === "!") throw unsupported("YAML tags (!tag)", line);
  if (ch === "|" || ch === ">") throw unsupported("block scalars (| or >)", line);
  if (ch === "%" || ch === "@" || ch === "`") throw new ManifestYamlError(`a value cannot start with ${ch}`, line);
}

function setKey(target: { [key: string]: YamlValue }, key: YamlValue, value: YamlValue, line: number): void {
  if (typeof key !== "string") throw unsupported(`the key ${JSON.stringify(key)} is not text`, line);
  if (Object.prototype.hasOwnProperty.call(target, key)) throw new ManifestYamlError(`duplicate key "${key}"`, line);
  Object.defineProperty(target, key, { value, enumerable: true, writable: true, configurable: true });
}

/* --------------------------------------------------------------- block */

class Block {
  i = 0;
  constructor(readonly lines: Line[]) {}

  get current(): Line | undefined {
    return this.lines[this.i];
  }

  /** A flow collection that may continue on the next lines. */
  flow(text: string, line: Line): YamlValue {
    let joined = text;
    for (;;) {
      if (balanced(joined)) break;
      const next = this.lines[this.i];
      if (next === undefined) throw new ManifestYamlError("a [ or { is not closed", line.no);
      // "\n" keeps the line boundary visible to the flow reader (keys and values must not span it).
      joined += `\n${next.text}`;
      this.i += 1;
    }
    const parser = new Flow(joined, line.no);
    const value = parser.value();
    parser.skip();
    if (parser.i < joined.length) throw new ManifestYamlError("unexpected text after a flow collection", line.no);
    return value;
  }

  /** The value written after `key:` or `- ` on the same line. */
  inline(text: string, line: Line): YamlValue {
    const first = text[0] ?? "";
    if (first === "[" || first === "{") return this.flow(text, line);
    if (first === '"' || first === "'") {
      const [value, end] = readQuoted(text, 0, line.no);
      if (trimWs(text.slice(end)) !== "") throw new ManifestYamlError("unexpected text after a quoted string", line.no);
      return value;
    }
    checkIndicator(first, line.no);
    if (isItem(text)) throw new ManifestYamlError("a list cannot start on the same line as its key (`key: - x`)", line.no);
    if (/:([ \t]|$)/.test(text)) throw new ManifestYamlError("mapping values are not allowed here", line.no);
    return resolvePlain(text, line.no);
  }

  /** The value of `key:` with nothing after it: a nested block or null. */
  nested(parentIndent: number, allowSequenceAtSameIndent: boolean): YamlValue {
    const next = this.current;
    if (next === undefined) return null;
    if (next.indent > parentIndent) return this.block(next.indent);
    if (allowSequenceAtSameIndent && next.indent === parentIndent && isItem(next.text)) {
      return this.sequence(parentIndent);
    }
    return null;
  }

  block(indent: number): YamlValue {
    const line = this.current;
    if (line === undefined) return null;
    return isItem(line.text) ? this.sequence(indent) : this.mapping(indent);
  }

  sequence(indent: number): YamlValue[] {
    const out: YamlValue[] = [];
    for (;;) {
      const line = this.current;
      if (line === undefined || line.indent < indent) return out;
      if (line.indent > indent) throw new ManifestYamlError("bad indentation", line.no);
      if (!isItem(line.text)) return out;
      const rest = line.text.slice(1).replace(/^[ \t]+/, "");
      const column = line.indent + (line.text.length - rest.length);
      this.i += 1;
      if (rest === "") {
        out.push(this.nested(indent, false));
      } else if (isItem(rest)) {
        throw unsupported("a list directly inside a list item (- - x)", line.no);
      } else if (keyColon(rest) !== null) {
        // `- key: v` starts a mapping whose keys sit at `column`.
        this.i -= 1;
        this.lines[this.i] = { no: line.no, indent: column, text: rest };
        out.push(this.mapping(column));
      } else {
        out.push(this.inline(rest, line));
      }
    }
  }

  mapping(indent: number): { [key: string]: YamlValue } {
    const out: { [key: string]: YamlValue } = {};
    for (;;) {
      const line = this.current;
      if (line === undefined || line.indent < indent) return out;
      if (line.indent > indent) throw new ManifestYamlError("bad indentation", line.no);
      if (isItem(line.text)) throw new ManifestYamlError("a list item where a key was expected", line.no);
      const split = keyColon(line.text);
      if (split === null) {
        if (line.text.startsWith("?")) throw unsupported("complex keys (? key)", line.no);
        throw new ManifestYamlError("expected `key: value`", line.no);
      }
      const [key, rest] = split;
      this.i += 1;
      const value = rest === "" ? this.nested(indent, true) : this.inline(rest, line);
      setKey(out, key, value, line.no);
    }
  }
}

function isItem(text: string): boolean {
  return text === "-" || /^-[ \t]/.test(text);
}

/** `key: rest` -> [key, rest]; null when the line is not a mapping entry. */
function keyColon(text: string): [YamlValue, string] | null {
  const first = text[0] ?? "";
  if (first === '"' || first === "'") {
    let end: number;
    let key: string;
    try {
      [key, end] = readQuoted(text, 0, 0);
    } catch {
      return null;
    }
    const after = text.slice(end).replace(/^[ \t]+/, "");
    if (!after.startsWith(":") || !/^:([ \t]|$)/.test(after)) return null;
    return [key, trimWs(after.slice(1))];
  }
  // Characters that cannot start a plain key (PyYAML `check_plain`): not a mapping entry.
  if ("[{?&*!,]}%@`|>#".includes(first)) return null;
  const match = /:([ \t]|$)/.exec(text);
  if (match === null) return null;
  const rawKey = trimWs(text.slice(0, match.index));
  return [resolvePlain(rawKey, 0), trimWs(text.slice(match.index + 1))];
}

/** Are the brackets of a flow collection closed (outside quotes)? */
function balanced(text: string): boolean {
  let depth = 0;
  let i = 0;
  while (i < text.length) {
    const ch = text[i] ?? "";
    if ((ch === '"' || ch === "'") && quoteStarts(text, i)) {
      try {
        i = readQuoted(text, i, 0)[1];
      } catch {
        return false;
      }
      continue;
    }
    if (ch === "[" || ch === "{") depth += 1;
    if (ch === "]" || ch === "}") depth -= 1;
    i += 1;
  }
  return depth <= 0;
}

/**
 * Parse a `compute.yaml` text: the document (null when empty). Throws
 * ManifestYamlError (`unsupported` = valid-looking YAML the browser does
 * not read; submit with the CLI).
 */
export function parseManifestYaml(text: string): YamlValue {
  try {
    return parseDocument(text);
  } catch (error) {
    // A RangeError (bad code point, nesting deep enough to exhaust the stack) is still a refusal.
    if (error instanceof RangeError) throw new ManifestYamlError(`this YAML cannot be read (${error.message})`);
    throw error;
  }
}

function parseDocument(text: string): YamlValue {
  checkCharacters(text.replace(/^\uFEFF/, ""));
  const lines = splitLinesOf(text);
  if (lines.length === 0) return null;
  const parser = new Block(lines);
  const first = lines[0];
  if (first === undefined) return null;
  let value: YamlValue;
  if (first.text.startsWith("[") || first.text.startsWith("{") || /^["']/.test(first.text) && keyColon(first.text) === null) {
    parser.i = 1;
    value = parser.inline(first.text, first);
  } else if (isItem(first.text) || keyColon(first.text) !== null) {
    value = parser.block(first.indent);
  } else {
    parser.i = 1;
    value = parser.inline(first.text, first);
  }
  const extra = parser.current;
  if (extra !== undefined) throw new ManifestYamlError("unexpected content (check the indentation)", extra.no);
  return value;
}
