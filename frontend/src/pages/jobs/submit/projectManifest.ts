/**
 * The job manifest the browser submits, read like `compute run` reads it
 * (common/manifest.py `load_manifest`): `compute.yaml` from the project root
 * (at most 64 KiB, UTF-8, BOM allowed) or nothing; a missing `name` is the
 * folder's name made valid (`sanitize_name`); a missing `entrypoint` is
 * `python main.py`, else `python __main__.py`; `env.requirements` must be a
 * file in the project. The controller validates the result with the real
 * manifest model - the browser only does what needs the project's files.
 */
import { ManifestYamlError, parseManifestYaml, type YamlValue } from "./manifestYaml";
import { ProjectError, type ProjectSource } from "./projectWalk";
import { SPEC } from "./spec";

export class ManifestError extends Error {
  override readonly name = "ManifestError";
  /** True when the CLI could submit it but the browser cannot read it. */
  readonly useCli: boolean;
  constructor(message: string, useCli = false) {
    super(`${SPEC.manifest_file}: ${message}`);
    this.useCli = useCli;
  }
}

/** common/manifest.py `sanitize_name`. */
export function sanitizeName(text: string): string {
  const cleaned = text
    .replace(/[^A-Za-z0-9._-]+/g, "-")
    .replace(/^[-._]+|[-._]+$/g, "")
    .slice(0, SPEC.max_name_chars);
  return cleaned || "job";
}

function isMapping(value: YamlValue | undefined): value is { [key: string]: YamlValue } {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Normalised POSIX path inside the project, or null when it leaves it. */
function insideProject(relative: string): string | null {
  if (relative.startsWith("/") || /^[A-Za-z]:/.test(relative)) return null;
  const parts: string[] = [];
  for (const part of relative.split("/")) {
    if (part === "" || part === ".") continue;
    if (part === "..") {
      if (parts.length === 0) return null;
      parts.pop();
    } else parts.push(part);
  }
  return parts.join("/");
}

export interface ManifestResult {
  manifest: Record<string, YamlValue>;
  /** True when the project has a compute.yaml (else everything was inferred). */
  fromFile: boolean;
}

export async function readProjectManifest(source: ProjectSource): Promise<ManifestResult> {
  const byPath = new Map(source.files.map((f) => [f.path, f]));
  const file = byPath.get(SPEC.manifest_file);
  let document: YamlValue = null;
  if (file !== undefined) {
    if (file.blob.size > SPEC.max_manifest_file_bytes) {
      throw new ManifestError(`larger than ${SPEC.max_manifest_file_bytes} bytes`);
    }
    let text: string;
    try {
      text = new TextDecoder("utf-8", { fatal: true }).decode(await file.blob.arrayBuffer());
    } catch {
      throw new ManifestError("not UTF-8 text");
    }
    try {
      document = parseManifestYaml(text);
    } catch (error) {
      if (error instanceof ManifestYamlError) throw new ManifestError(`not valid YAML: ${error.message}`, error.unsupported);
      throw error;
    }
  }
  if (document === null) document = {};
  if (!isMapping(document)) throw new ManifestError("the manifest must be a mapping of keys");
  const manifest: Record<string, YamlValue> = { ...document };
  if (!("name" in manifest)) manifest.name = sanitizeName(source.name);
  if (!("entrypoint" in manifest)) {
    const found = SPEC.inferred_entrypoints.find((candidate) => byPath.has(candidate));
    if (found === undefined) {
      throw new ManifestError(
        `entrypoint: missing, and it cannot be inferred: looked for ${SPEC.inferred_entrypoints.join(", then ")} ` +
          `in the project folder (or add entrypoint: to ${SPEC.manifest_file})`,
      );
    }
    manifest.entrypoint = ["python", found];
  }
  const env = manifest.env;
  if (isMapping(env) && typeof env.requirements === "string") {
    const inside = insideProject(env.requirements);
    if (inside === null) throw new ManifestError("env.requirements: points outside the project folder");
    if (!byPath.has(inside)) throw new ManifestError("env.requirements: no such file in the project folder");
  }
  return { manifest, fromFile: file !== undefined };
}

export { ProjectError };
