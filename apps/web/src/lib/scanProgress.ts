export function scanProgress(status?: string): { step: number; label: string } {
  if (status === "failed") return { step: -1, label: "Scan failed. Please retry." };
  if (status === "complete") return { step: 3, label: "Scan finished" };
  if (status === "indexing") return { step: 2, label: "Preparing results" };
  if (status === "ocr_processing" || status === "normalizing") return { step: 1, label: "Reading dishes" };
  if (status === "needs_background_refresh") return { step: 0, label: "Still processing in the background" };
  return { step: 0, label: status === "queued" ? "Waiting to scan" : "Finding menu" };
}
