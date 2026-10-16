/**
 * A folder picked with `<input type="file" webkitdirectory>` as a project
 * source: every file's `webkitRelativePath` is `<folder>/<path inside>`; the
 * folder's name is the default job name (as `compute run <folder>`).
 */
import { ProjectError, type ProjectSource } from "./projectWalk";

export function folderSource(files: readonly File[]): ProjectSource {
  if (files.length === 0) throw new ProjectError("the picked folder is empty (or the browser listed no files)");
  let root: string | null = null;
  const out: ProjectSource["files"] = [];
  for (const file of files) {
    const rel = file.webkitRelativePath || file.name;
    const cut = rel.indexOf("/");
    const top = cut < 0 ? "" : rel.slice(0, cut);
    if (root === null) root = top;
    if (top !== root || cut < 0) throw new ProjectError("pick one folder (the files do not share a folder)", rel);
    out.push({ path: rel.slice(cut + 1), blob: file, executable: false });
  }
  return { name: root ?? "job", files: out, dirs: [] };
}
