import type { CatalogItem } from "./types";

export function compareSavedAt(left: CatalogItem, right: CatalogItem) {
  // Older static catalogs only expose the source date and import time.
  const leftTimestamp = left.saved_at || left.date || left.added_at || "";
  const rightTimestamp = right.saved_at || right.date || right.added_at || "";
  const leftDate = Date.parse(leftTimestamp);
  const rightDate = Date.parse(rightTimestamp);
  const difference = (Number.isFinite(rightDate) ? rightDate : 0) - (Number.isFinite(leftDate) ? leftDate : 0);
  if (difference) return difference;
  if (Number.isFinite(leftDate) && Number.isFinite(rightDate)) {
    // Date.parse drops the database's sub-millisecond precision.
    const leftFraction = (leftTimestamp.match(/\.(\d+)/)?.[1] || "").padEnd(6, "0");
    const rightFraction = (rightTimestamp.match(/\.(\d+)/)?.[1] || "").padEnd(6, "0");
    const fractionDifference = rightFraction.localeCompare(leftFraction);
    if (fractionDifference) return fractionDifference;
  }
  return right.id - left.id;
}

export function mergeCatalogItems(current: CatalogItem[], incoming: CatalogItem[]) {
  const items = new Map(current.map((item) => [item.id, item]));
  for (const item of incoming) items.set(item.id, item);
  return [...items.values()].sort(compareSavedAt);
}
