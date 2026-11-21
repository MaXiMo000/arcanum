// What every view of the database in the middle of the screen (the 2D engine map, the 3D vault) is given.
import type { EngineState, Tree } from "./api";
import type { FeedState } from "./feed";
import type { PageOwner } from "./format";

export interface StageProps {
  trees: Tree[];
  selected: string | null;
  onSelect: (name: string) => void;
  /** The engine events being played back. */
  feed: FeedState;
  /** How long a page or node stays highlighted after an event touches it. */
  flashMs: number;
  state: EngineState;
  /** Which tree (or table's overflow pages) each page belongs to. */
  owners: Map<number, PageOwner>;
}
