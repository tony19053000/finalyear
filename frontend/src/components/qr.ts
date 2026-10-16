/**
 * QR module matrix and its SVG path (V0.8 G5, D199). `qrcode-generator` only
 * computes the matrix; `QrCode.tsx` draws it as one SVG <path> (CSP-safe).
 */
import qrcode from "qrcode-generator";

/** Quiet zone in modules (the QR standard's minimum). */
export const QR_MARGIN = 4;

/** The module matrix of `text` (byte mode, error correction M). `true` = dark. */
export function qrModules(text: string): boolean[][] {
  if (!/^[\x20-\x7e]*$/.test(text)) {
    // Join codes are ASCII; byte mode here is Latin-1, so refuse rather than mis-encode.
    throw new Error("qrModules: only printable ASCII is encoded");
  }
  const qr = qrcode(0, "M");
  qr.addData(text, "Byte");
  qr.make();
  const count = qr.getModuleCount();
  return Array.from({ length: count }, (_, row) => Array.from({ length: count }, (_, col) => qr.isDark(row, col)));
}

/** One path: a 1x1 square per dark module, offset by the quiet zone. */
export function qrPath(modules: readonly (readonly boolean[])[], margin: number = QR_MARGIN): string {
  const parts: string[] = [];
  modules.forEach((row, y) => {
    row.forEach((dark, x) => {
      if (dark) parts.push(`M${String(x + margin)} ${String(y + margin)}h1v1h-1z`);
    });
  });
  return parts.join("");
}
