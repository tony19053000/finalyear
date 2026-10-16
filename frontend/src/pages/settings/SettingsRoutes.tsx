/** The Settings section's routes (V0.8 G5): `/settings/*`, one lazy chunk. */
import { Route, Routes } from "react-router-dom";

import { NotFoundPage } from "../PlaceholderPages";
import { SettingsPage } from "../SettingsPage";

export function SettingsRoutes() {
  return (
    <Routes>
      <Route index element={<SettingsPage />} />
      <Route path="*" element={<NotFoundPage />} />
    </Routes>
  );
}
