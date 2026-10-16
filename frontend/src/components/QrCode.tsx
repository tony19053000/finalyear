/**
 * A QR code drawn as ONE SVG <path> (V0.8 G5, D199): no canvas, no data: URL,
 * no inline style - colours are SVG presentation attributes, which the
 * controller's CSP (`style-src 'self'`, `img-src 'self'`) does not govern.
 *
 * The code is dark-on-white with a 4-module quiet zone in both themes:
 * phone scanners need that contrast and margin.
 */
import { QR_MARGIN, qrModules, qrPath } from "./qr";

export interface QrCodeProps {
  text: string;
  /** Accessible name, e.g. "QR code of the join code". */
  label: string;
  /** Rendered size in CSS pixels (width = height). */
  size?: number;
  className?: string;
}

export function QrCode({ text, label, size = 224, className }: QrCodeProps) {
  const modules = qrModules(text);
  const side = modules.length + 2 * QR_MARGIN;
  return (
    <svg
      role="img"
      aria-label={label}
      viewBox={`0 0 ${String(side)} ${String(side)}`}
      width={size}
      height={size}
      shapeRendering="crispEdges"
      data-modules={modules.length}
      data-margin={QR_MARGIN}
      className={className}
    >
      <rect width={side} height={side} fill="#ffffff" />
      <path d={qrPath(modules)} fill="#000000" />
    </svg>
  );
}
