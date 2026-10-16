import { useEffect } from "react";

import { PRODUCT_NAME } from "../config";

/** `<title>` = "<page> · MeshForge" (tabs and screen readers name the page). */
export function useDocumentTitle(page: string): void {
  useEffect(() => {
    document.title = `${page} · ${PRODUCT_NAME}`;
  }, [page]);
}
