// Browser storage that never throws: it can be unavailable (private windows, blocked site data), and
// everything kept there is a convenience the page works without.

export type StorageArea = () => Storage;

/** Kept across visits (preferences, progress). */
export const local: StorageArea = () => window.localStorage;
/** Kept for this tab only (its database session). */
export const tab: StorageArea = () => window.sessionStorage;

export function readStorage(area: StorageArea, key: string): string | null {
  try {
    return area().getItem(key);
  } catch {
    return null;
  }
}

export function writeStorage(area: StorageArea, key: string, value: string | null): void {
  try {
    if (value === null) area().removeItem(key);
    else area().setItem(key, value);
  } catch {
    // Not essential: the page works without it.
  }
}

/** Parsed JSON from storage, or null when it is missing or damaged. */
export function readJson(area: StorageArea, key: string): unknown {
  try {
    return JSON.parse(readStorage(area, key) ?? "null");
  } catch {
    return null;
  }
}
