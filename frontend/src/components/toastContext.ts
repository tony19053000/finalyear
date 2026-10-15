import { createContext, useContext } from "react";

import type { StatusTone } from "../lib/status";

export interface ToastInput {
  title: string;
  description?: string;
  tone?: Extract<StatusTone, "ok" | "failed"> | "info";
}

export type Notify = (toast: ToastInput) => void;

export const ToastContext = createContext<Notify | null>(null);

/** `const toast = useToast(); toast({ title: "Job cancelled", tone: "ok" })`. */
export function useToast(): Notify {
  const notify = useContext(ToastContext);
  if (notify === null) {
    throw new Error("useToast() needs a <ToastProvider> above it");
  }
  return notify;
}
