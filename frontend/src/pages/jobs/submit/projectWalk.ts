/**
 * Which files of a picked folder (or zip) become the job, by the CLI's rules
 * (common/walk.py `scan_tree`, D81): the built-in ignores, then every
 * `.gitignore` and `.computeignore` in the tree with git's semantics (a nested
 * file applies below its folder, a later rule wins, an ignored folder is not
 * descended into), the same path refusals and the same caps. The constants
 * come from the ONE shared spec, projectSpec.json.
 *
 * Browser differences (stated in the submit page): a folder picker shows no
 * empty folders (a zip does) and follows symlinks (the CLI records them as
 * links); the browser cannot see the exec bit of a picked file (a zip made on
 * Linux/macOS carries it), so such a file reads `exec: false` - as a project
 * submitted from Windows does.
 */
import { IgnoreSpec, IgnorePatternError, rebase, splitLines } from "./gitignore";
import { SPEC } from "./spec";

export interface SourceFile {
  /** Relative POSIX path inside the project (the picked folder's name stripped). */
  path: string;
  blob: Blob;
  executable: boolean;
}

export interface ProjectSource {
  /** The picked folder's (or zip's) name: the default job name. */
  name: string;
  files: SourceFile[];
  /** Folders listed explicitly (zip entries); others are implied by files. */
  dirs: string[];
}

export type WalkEntry =
  | { kind: "file"; path: string; size: number; executable: boolean; blob: Blob }
  | { kind: "dir"; path: string };

export interface WalkResult {
  entries: WalkEntry[];
  ignored: number;
  totalBytes: number;
}

/** A project that cannot be submitted as it is (the CLI refuses it too). */
export class ProjectError extends Error {
  override readonly name = "ProjectError";
  readonly path: string | undefined;
  constructor(message: string, path?: string) {
    super(path === undefined ? message : `${message}: ${shownPath(path)}`);
    this.path = path;
  }
}

const encoder = new TextEncoder();

export function utf8Length(text: string): number {
  return encoder.encode(text).length;
}

function shownPath(path: string): string {
  const flat = Array.from(path).map((ch) => (/\p{Cc}|\p{Cf}/u.test(ch) ? "?" : ch)).join("");
  return flat.length <= 200 ? flat : `${flat.slice(0, 197)}...`;
}

/** Code-point order (Python's `sorted` on str), not UTF-16 order. */
export function compareCodePoints(a: string, b: string): number {
  const x = Array.from(a);
  const y = Array.from(b);
  for (let i = 0; i < Math.min(x.length, y.length); i += 1) {
    const d = (x[i]?.codePointAt(0) ?? 0) - (y[i]?.codePointAt(0) ?? 0);
    if (d !== 0) return d;
  }
  return x.length - y.length;
}

/** common/walk.py `check_rel` + the archive-name rules the controller applies. */
export function checkRel(rel: string): void {
  if (rel === "" || rel.includes("\0")) throw new ProjectError("an empty name or a NUL byte in a name", rel);
  if (/\p{Cs}/u.test(rel)) throw new ProjectError("a file name is not valid UTF-8 and cannot be bundled portably", rel);
  if (utf8Length(rel) > SPEC.max_path_bytes) throw new ProjectError(`path longer than ${SPEC.max_path_bytes} bytes`, rel);
  const last = rel.split("/").pop() ?? "";
  if (utf8Length(last) > SPEC.max_component_bytes) {
    throw new ProjectError(`name longer than ${SPEC.max_component_bytes} bytes`, rel);
  }
  if (rel.includes("\\")) throw new ProjectError("a file name contains '\\' (not portable)", rel);
  if (rel.startsWith("/") || /^[A-Za-z]:/.test(rel)) throw new ProjectError("absolute path", rel);
  for (const part of rel.split("/")) {
    if (part === "" || part === "." || part === "..") throw new ProjectError("a path has an empty, '.' or '..' part", rel);
  }
}

interface Folder {
  files: Map<string, SourceFile>;
  dirs: Set<string>;
}

function folderTree(source: ProjectSource): Map<string, Folder> {
  const folders = new Map<string, Folder>([["", { files: new Map(), dirs: new Set() }]]);
  const ensure = (dir: string): Folder => {
    let folder = folders.get(dir);
    if (folder !== undefined) return folder;
    folder = { files: new Map(), dirs: new Set() };
    folders.set(dir, folder);
    const cut = dir.lastIndexOf("/");
    const parent = cut < 0 ? "" : dir.slice(0, cut);
    ensure(parent).dirs.add(cut < 0 ? dir : dir.slice(cut + 1));
    return folder;
  };
  for (const dir of source.dirs) {
    const clean = dir.replace(/\/+$/, "");
    if (clean !== "") {
      checkRel(clean);
      ensure(clean);
    }
  }
  for (const file of source.files) {
    checkRel(file.path);
    const cut = file.path.lastIndexOf("/");
    const parent = cut < 0 ? "" : file.path.slice(0, cut);
    const name = cut < 0 ? file.path : file.path.slice(cut + 1);
    const folder = ensure(parent);
    if (folder.files.has(name) || folders.has(file.path)) {
      throw new ProjectError("the same path appears twice", file.path);
    }
    folder.files.set(name, file);
  }
  return folders;
}

async function ignoreLines(folder: Folder, relDir: string): Promise<string[]> {
  const lines: string[] = [];
  for (const name of SPEC.ignore_file_names) {
    const file = folder.files.get(name);
    if (file === undefined) continue;
    if (file.blob.size > SPEC.max_ignore_file_bytes) {
      throw new ProjectError(`${name} is larger than ${SPEC.max_ignore_file_bytes} bytes`, relDir + name);
    }
    let text: string;
    try {
      text = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(await file.blob.arrayBuffer());
    } catch {
      throw new ProjectError(`${name} is not UTF-8`, relDir + name);
    }
    for (const raw of splitLines(text.replace(/^\uFEFF/, ""))) {
      const line = rebase(raw, relDir);
      if (line !== null) lines.push(line);
    }
  }
  return lines;
}

/** List the project (see the module comment); entries sorted by path. */
export async function walkProject(source: ProjectSource): Promise<WalkResult> {
  const folders = folderTree(source);
  const maxListed = SPEC.max_entries * 8;
  const entries: WalkEntry[] = [];
  let ignored = 0;
  let totalBytes = 0;
  const add = (entry: WalkEntry) => {
    entries.push(entry);
    if (entries.length > maxListed) {
      throw new ProjectError(`more than ${maxListed} files and folders (add rules to .computeignore)`);
    }
    if (entry.kind === "file") {
      totalBytes += entry.size;
      if (totalBytes > SPEC.max_total_bytes) {
        throw new ProjectError(`project is larger than ${SPEC.max_total_bytes} bytes (add rules to .computeignore)`);
      }
    }
  };
  const stack: [string, string[]][] = [["", [...SPEC.builtin_ignores]]];
  for (let top = stack.pop(); top !== undefined; top = stack.pop()) {
    const [relDir, inherited] = top;
    const folder: Folder = folders.get(relDir.replace(/\/$/, "")) ?? { files: new Map<string, SourceFile>(), dirs: new Set<string>() };
    const rules = [...inherited, ...(await ignoreLines(folder, relDir))];
    let spec: IgnoreSpec;
    try {
      spec = IgnoreSpec.fromLines(rules);
    } catch (error) {
      if (error instanceof IgnorePatternError) throw new ProjectError(error.message, relDir || ".");
      throw error;
    }
    const names = [...new Set([...folder.files.keys(), ...folder.dirs])].sort(compareCodePoints);
    for (const name of names) {
      const rel = relDir + name;
      const file = folder.files.get(name);
      if (file === undefined) {
        if (spec.matches(`${rel}/`)) {
          ignored += 1;
          continue;
        }
        add({ kind: "dir", path: rel });
        stack.push([`${rel}/`, rules]);
      } else {
        if (spec.matches(rel)) {
          ignored += 1;
          continue;
        }
        add({ kind: "file", path: rel, size: file.blob.size, executable: file.executable, blob: file.blob });
      }
    }
  }
  entries.sort((a, b) => compareCodePoints(a.path, b.path));
  return { entries, ignored, totalBytes };
}
