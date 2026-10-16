/**
 * Git ignore-pattern matching, ported from the CLI's matcher - `pathspec`'s
 * `GitIgnoreSpec` (pathspec 1.x: patterns/gitignore/spec.py, base.py and
 * _backends/simple/gitignore.py) as used by common/walk.py - so the browser
 * and `compute run` ignore exactly the same files. Shared vectors in
 * projectSpec.json test both sides.
 *
 * Semantics kept from pathspec: a blank line or `#...` is no rule; trailing
 * spaces are dropped unless escaped (`\ `); `!` negates; a leading `/` or a
 * slash inside anchors the pattern; a trailing `/` matches only folders (the
 * walk asks about a folder as `path/`); `*`, `?`, `[...]` within one path
 * segment, `**` across segments; an invalid `[` range drops the rule (as git
 * does); a match against a folder marker (`foo/` matched by `foo/**`) loses
 * to ANY direct match, earlier or later; among folder-marker matches the last
 * wins (pathspec's priority rule, evaluated in its default reversed order).
 */

export class IgnorePatternError extends Error {
  override readonly name = "IgnorePatternError";
}

class RangeError_ extends Error {}

export interface CompiledRule {
  regex: RegExp;
  /** true = ignore (exclude from the job), false = a `!` re-include. */
  include: boolean;
}

const DIR_MARK = "ps_d";
const DIR_MARK_CG = `(?<${DIR_MARK}>/)`;
const DIR_MARK_OPT = `(?:${DIR_MARK_CG}|$)`;

/** Python `re.escape` for one character, valid in a JS `u` regex. */
function escapeChar(char: string): string {
  return /[\\^$.*+?()[\]{}|/]/.test(char) ? `\\${char}` : char;
}

/** base.py `_translate_segment_glob(pattern, "raise")`. */
function translateSegmentGlob(pattern: string): string {
  const chars = Array.from(pattern);
  let escape = false;
  let regex = "";
  let i = 0;
  const end = chars.length;
  while (i < end) {
    const char = chars[i] ?? "";
    i += 1;
    if (escape) {
      escape = false;
      regex += escapeChar(char);
    } else if (char === "\\") {
      escape = true;
    } else if (char === "*") {
      regex += "[^/]*";
    } else if (char === "?") {
      regex += "[^/]";
    } else if (char === "[") {
      let j = i;
      if (j < end && (chars[j] === "!" || chars[j] === "^")) j += 1;
      if (j < end && chars[j] === "]") j += 1;
      while (j < end && chars[j] !== "]") j += 1;
      if (j < end) {
        j += 1;
        let expr = "[";
        if (chars[i] === "!" || chars[i] === "^") {
          expr += "^";
          i += 1;
        }
        expr += chars.slice(i, j).join("").replace(/\\/g, "\\\\");
        regex += expr;
        i = j;
      } else {
        throw new RangeError_("invalid range");
      }
    } else {
      regex += escapeChar(char);
    }
  }
  if (escape) throw new IgnorePatternError(`a pattern ends with an escape character: ${pattern}`);
  return regex;
}

/** spec.py `__normalize_segments`: [segments, override regex]. */
function normalizeSegments(isDirPattern: boolean, segs: string[]): [string[] | null, string | null] {
  if (segs[0] === "") {
    segs.shift();
  } else if (segs.length === 1 || (segs.length === 2 && segs[1] === "")) {
    if (segs[0] !== "**") segs.unshift("**");
  }
  if (segs.length === 0) throw new IgnorePatternError("a pattern normalised to nothing");
  if (segs[segs.length - 1] === "") segs[segs.length - 1] = "**";
  for (let i = segs.length - 1; i > 0; i -= 1) {
    if (segs[i - 1] === "**" && segs[i] === "**") segs.splice(i, 1);
  }
  if (segs.length === 1 && segs[0] === "**") return [null, isDirPattern ? DIR_MARK_CG : "."];
  if (segs.length === 2 && segs[0] === "**" && segs[1] === "*") return [null, "."];
  if (segs.length === 3 && segs[0] === "**" && segs[1] === "*" && segs[2] === "**") {
    return [null, isDirPattern ? DIR_MARK_CG : "/"];
  }
  return [segs, null];
}

/** spec.py `__translate_segments`. */
function translateSegments(isDirPattern: boolean, segs: string[]): string {
  const out: string[] = [];
  let needSlash = false;
  const end = segs.length - 1;
  segs.forEach((seg, i) => {
    if (seg === "**") {
      if (i === 0) {
        out.push("^(?:.+/)?");
      } else if (i < end) {
        out.push("(?:/.+)?");
        needSlash = true;
      } else {
        out.push(isDirPattern ? DIR_MARK_CG : "/");
      }
    } else {
      if (i === 0) out.push("^");
      if (needSlash) out.push("/");
      out.push(seg === "*" ? "[^/]+" : translateSegmentGlob(seg));
      if (i === end) out.push(DIR_MARK_OPT);
      needSlash = true;
    }
  });
  return out.join("");
}

/** One gitignore line -> a rule, or null (blank, comment, `/`, invalid range). */
export function compilePattern(line: string): CompiledRule | null {
  let pattern = line;
  if (!pattern.endsWith("\\ ")) pattern = pattern.replace(/\s+$/u, "");
  if (pattern === "" || pattern.startsWith("#") || pattern === "/") return null;
  let include = true;
  if (pattern.startsWith("!")) {
    include = false;
    pattern = pattern.slice(1);
  }
  const segs = pattern.split("/");
  const isDirPattern = segs[segs.length - 1] === "";
  const [normalized, override] = normalizeSegments(isDirPattern, segs);
  let source: string;
  if (override !== null) {
    source = override;
  } else {
    try {
      source = translateSegments(isDirPattern, normalized ?? []);
    } catch (error) {
      if (error instanceof RangeError_) return null; // git drops it
      throw error;
    }
  }
  try {
    return { regex: new RegExp(source, "u"), include };
  } catch {
    // A bracket expression JS cannot express the way Python's `re` reads it.
    throw new IgnorePatternError(`unsupported ignore pattern: ${line}`);
  }
}

/** A compiled rule list; `matches(path)` = ignore it (folders end with `/`). */
export class IgnoreSpec {
  private constructor(private readonly rules: readonly CompiledRule[]) {}

  static fromLines(lines: readonly string[]): IgnoreSpec {
    const rules: CompiledRule[] = [];
    for (const line of lines) {
      const rule = compilePattern(line);
      if (rule !== null) rules.push(rule);
    }
    return new IgnoreSpec(rules);
  }

  /**
   * _backends/simple/gitignore.py `SimpleGiBackend.match_file` in the order
   * `GitIgnoreSpec` really uses: REVERSED (`no_reverse` defaults to None, so
   * `_is_reversed` is true). Walking from the last rule back, a direct
   * (non-folder-marker) match wins at once; otherwise the LAST folder-marker
   * match counts. So any direct match - even an earlier `!x` - beats a later
   * `dir/**` match (T-6, T-7: the forward variant disagreed with the CLI).
   */
  matches(path: string): boolean {
    let file = path;
    if (file.startsWith("/")) file = file.slice(1);
    else if (file.startsWith("./")) file = file.slice(2);
    let outInclude: boolean | null = null;
    let outPriority = 0;
    for (let i = this.rules.length - 1; i >= 0; i -= 1) {
      const rule = this.rules[i];
      if (rule === undefined) continue;
      const match = rule.regex.exec(file);
      if (match === null) continue;
      const priority = match.groups?.[DIR_MARK] ? 1 : 2;
      if (priority > outPriority) {
        outInclude = rule.include;
        outPriority = priority;
      }
      if (priority === 2) break;
    }
    return outInclude === true;
  }
}

/** common/walk.py `_escape_glob`: a folder name used as a pattern prefix. */
export function escapeGlob(text: string): string {
  return text.replace(/([[\]*?!#\\])/g, "\\$1");
}

/** common/walk.py `_rebase`: a line of `<relDir>.gitignore` rewritten relative to the root. */
export function rebase(rawLine: string, relDir: string): string | null {
  const line = rawLine.replace(/[\r\n]+$/, "");
  if (line.trim() === "" || line.startsWith("#")) return null;
  if (relDir === "") return line;
  const negate = line.startsWith("!");
  const body = negate ? line.slice(1) : line;
  const prefix = escapeGlob(relDir);
  const anchored = body.replace(/\/+$/, "").includes("/");
  const rebased = anchored ? prefix + body.replace(/^\/+/, "") : `${prefix}**/${body}`;
  return (negate ? "!" : "") + rebased;
}

// Built from a string: the control characters Python counts as line ends.
// eslint-disable-next-line no-control-regex -- Python splitlines() splits on these
const LINE_BREAKS = new RegExp("\\r\\n|[\\n\\r\\v\\f\\x1c\\x1d\\x1e\\x85\\u2028\\u2029]", "u");

/** Python `str.splitlines()` (every line boundary it knows). */
export function splitLines(text: string): string[] {
  const lines = text.split(LINE_BREAKS);
  if (lines.length > 0 && lines[lines.length - 1] === "") lines.pop();
  return lines;
}
