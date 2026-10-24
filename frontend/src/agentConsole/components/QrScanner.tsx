import jsQR from "jsqr";
import { useEffect, useRef, useState } from "react";

import { Button } from "../../components/Button";
import { joinCodeIn } from "../logic";

/** Scan frames this often (ms): plenty for a code held up to a webcam. */
const SCAN_INTERVAL_MS = 200;

export interface QrScannerProps {
  /** Called once with the join code read from the QR; the camera is off by then. */
  onCode: (code: string) => void;
  onCancel: () => void;
}

/**
 * Q6/Q25: scan the invite QR shown on the controller dashboard with this
 * laptop's webcam. Opt-in (the owner clicks "Scan"), runs only in the browser
 * (`getUserMedia` is allowed on http://127.0.0.1, a secure context), nothing is
 * uploaded, and the camera is released as soon as a code is read, on cancel and
 * when the page goes away. Pasting the code stays the main path.
 */
export function QrScanner({ onCode, onCancel }: QrScannerProps) {
  const video = useRef<HTMLVideoElement>(null);
  // Undefined outside a secure context (and in old browsers), whatever the types say.
  const supported = "mediaDevices" in navigator && typeof navigator.mediaDevices.getUserMedia === "function";
  const [problem, setProblem] = useState<string | null>(
    supported ? null : "This browser cannot use a camera here. Paste the code instead.",
  );
  const [hint, setHint] = useState("Starting the camera…");

  useEffect(() => {
    let stream: MediaStream | null = null;
    let timer: number | undefined;
    let done = false;
    const canvas = document.createElement("canvas");
    const context = canvas.getContext("2d", { willReadFrequently: true });

    const stop = () => {
      done = true;
      if (timer !== undefined) window.clearInterval(timer);
      stream?.getTracks().forEach((t) => {
        t.stop();
      });
      stream = null;
      if (video.current !== null) video.current.srcObject = null;
    };

    const scan = () => {
      const v = video.current;
      if (done || v === null || context === null || v.videoWidth === 0) return;
      canvas.width = v.videoWidth;
      canvas.height = v.videoHeight;
      context.drawImage(v, 0, 0, canvas.width, canvas.height);
      const image = context.getImageData(0, 0, canvas.width, canvas.height);
      const found = jsQR(image.data, image.width, image.height, { inversionAttempts: "attemptBoth" });
      if (found === null) return;
      const code = joinCodeIn(found.data);
      if (code === null) {
        setHint("That QR code is not a MeshForge join code. Show the one from the Invites page.");
        return;
      }
      stop();
      onCode(code);
    };

    if (!supported) return stop;
    navigator.mediaDevices
      .getUserMedia({ video: { facingMode: "environment" }, audio: false })
      .then(async (s) => {
        if (done) {
          s.getTracks().forEach((t) => {
            t.stop();
          });
          return;
        }
        stream = s;
        const v = video.current;
        if (v === null) return;
        v.srcObject = s;
        await v.play();
        setHint("Hold the QR code from the dashboard's Invites page up to the camera.");
        timer = window.setInterval(scan, SCAN_INTERVAL_MS);
      })
      .catch((err: unknown) => {
        const name = err instanceof DOMException ? err.name : "";
        setProblem(
          name === "NotAllowedError"
            ? "Camera access was not allowed. Paste the code instead, or allow the camera for this page."
            : name === "NotFoundError"
              ? "No camera was found on this laptop. Paste the code instead."
              : "The camera could not be started. Paste the code instead.",
        );
      });
    return stop;
  }, [onCode, supported]);

  return (
    <div className="flex flex-col gap-2" aria-label="QR code scanner">
      {problem === null ? (
        <>
          <video ref={video} className="aspect-video w-full max-w-md rounded-md bg-surface-sunken" muted playsInline />
          <p role="status" className="text-sm text-fg-muted">
            {hint}
          </p>
        </>
      ) : (
        <p role="alert" className="text-sm text-status-failed">
          {problem}
        </p>
      )}
      <div>
        <Button size="sm" onClick={onCancel}>
          {problem === null ? "Stop camera" : "Close"}
        </Button>
      </div>
    </div>
  );
}
