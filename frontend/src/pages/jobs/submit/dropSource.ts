/**
 * A folder dragged onto the page as a project source: the same shape as a
 * picked folder (folderSource.ts) - every file under the dropped folder, its
 * path inside it, the folder's name as the default job name. A folder picker
 * and a drop both see files only (no exec bits; empty folders are not kept,
 * as with the picker).
 */
import { ProjectError, type ProjectSource } from "./projectWalk";

/** Every entry of a dropped folder (the browser hands them out in batches). */
function readAll(dir: FileSystemDirectoryEntry): Promise<FileSystemEntry[]> {
  const reader = dir.createReader();
  const out: FileSystemEntry[] = [];
  return new Promise((resolve, reject) => {
    const next = () => {
      reader.readEntries((batch) => {
        if (batch.length === 0) {
          resolve(out);
          return;
        }
        out.push(...batch);
        next();
      }, reject);
    };
    next();
  });
}

function fileOf(entry: FileSystemFileEntry): Promise<File> {
  return new Promise((resolve, reject) => {
    entry.file(resolve, reject);
  });
}

export async function droppedFolderSource(root: FileSystemDirectoryEntry): Promise<ProjectSource> {
  const files: ProjectSource["files"] = [];
  const walk = async (dir: FileSystemDirectoryEntry, prefix: string): Promise<void> => {
    for (const entry of await readAll(dir)) {
      const path = `${prefix}${entry.name}`;
      if (entry.isFile) files.push({ path, blob: await fileOf(entry as FileSystemFileEntry), executable: false });
      else if (entry.isDirectory) await walk(entry as FileSystemDirectoryEntry, `${path}/`);
    }
  };
  await walk(root, "");
  if (files.length === 0) throw new ProjectError("the dropped folder is empty");
  return { name: root.name, files, dirs: [] };
}
