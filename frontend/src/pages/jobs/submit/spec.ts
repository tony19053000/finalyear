/** The shared project spec (projectSpec.json), typed. */
import raw from "./projectSpec.json";

export interface ProjectSpec {
  version: number;
  manifest_file: string;
  max_manifest_file_bytes: number;
  inferred_entrypoints: readonly string[];
  max_name_chars: number;
  builtin_ignores: readonly string[];
  ignore_file_names: readonly string[];
  max_ignore_file_bytes: number;
  max_path_bytes: number;
  max_component_bytes: number;
  tree_version: number;
  max_entries: number;
  max_total_bytes: number;
  max_tree_manifest_bytes: number;
  chunk_size: number;
  min_chunk_size: number;
  max_chunk_size: number;
  have_batch: number;
}

export const SPEC: ProjectSpec = raw;
