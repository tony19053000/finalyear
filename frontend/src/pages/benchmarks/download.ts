/**
 * Browser downloads of the campaign's Parquet files (V0.9 G6, D221).
 *
 * The file is fetched with the session cookie through `apiDownload` (same
 * CSRF / error rules as every call; nothing goes into a URL or storage), checked
 * to be a COMPLETE Parquet file, and handed to the browser as a blob link. The
 * check matters: the export streams its body after the 200, so an encoding
 * failure mid-way truncates it (no footer) - that must not be saved as if it
 * were the dataset.
 */
const MAGIC = "PAR1";
/** How long the object URL lives after the click. */
export const REVOKE_AFTER_MS = 30_000;

async function bytesAt(blob: Blob, start: number, end: number): Promise<string> {
  const buffer = await blob.slice(start, end).arrayBuffer();
  return String.fromCharCode(...new Uint8Array(buffer));
}

/** Starts and ends with the Parquet magic `PAR1` (and holds more than the two magics). */
export async function isCompleteParquet(blob: Blob): Promise<boolean> {
  if (blob.size < MAGIC.length * 2 + 4) return false;
  return (await bytesAt(blob, 0, MAGIC.length)) === MAGIC && (await bytesAt(blob, blob.size - MAGIC.length, blob.size)) === MAGIC;
}

export class IncompleteDownloadError extends Error {
  override readonly name = "IncompleteDownloadError";
  constructor(what: string) {
    super(`The ${what} download ended early (not a complete Parquet file); nothing was saved. Try again.`);
  }
}

/** Save `blob` as `filename` (a temporary object URL, revoked shortly after the click). */
export function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  try {
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    link.rel = "noopener";
    document.body.append(link);
    link.click();
    link.remove();
  } finally {
    // The click has started the download; give the browser time to read the blob.
    setTimeout(() => {
      URL.revokeObjectURL(url);
    }, REVOKE_AFTER_MS);
  }
}

export const recordsFileName = (campaignId: string) => `${campaignId}-records.parquet`;
export const labelsFileName = (campaignId: string) => `${campaignId}-labels.parquet`;
