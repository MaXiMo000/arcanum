// The data file as a floor of tiles, one per page, tinted by the tree that owns it. Cached pages glow;
// a disk read lifts a tile out of the dark, a cache hit flashes green, a write burns red and an
// eviction lets the tile sink.
import { useFrame, type ThreeEvent } from "@react-three/fiber";
import { memo, useEffect, useLayoutEffect, useMemo, useRef, type RefObject } from "react";
import { BoxGeometry, Color, InstancedMesh, MeshBasicMaterial, Object3D } from "three";
import type { FeedState } from "../feed";
import { COLORS, OWNER_COLORS, floorShape, glow, nextPowerOfTwo, tilePosition, type Motion } from "./world";

export interface PageInfo {
  owner?: { color: number };
  cached: boolean;
  dirty: boolean;
  free: boolean;
}

interface Props {
  pages: PageInfo[];
  feed: FeedState;
  flashMs: number;
  motion: RefObject<Motion>;
  onHover: (page: number | null, event?: ThreeEvent<PointerEvent>) => void;
}

const FLASH = { hit: COLORS.hit, miss: COLORS.miss, write: COLORS.write, evict: COLORS.evict } as const;

export const PageFloor = memo(function PageFloor({ pages, feed, flashMs, motion, onHover }: Props) {
  const capacity = nextPowerOfTwo(pages.length);
  const tiles = useRef<InstancedMesh>(null);
  const geometry = useMemo(() => new BoxGeometry(0.28, 0.07, 0.28), []);
  const material = useMemo(() => new MeshBasicMaterial({ toneMapped: false }), []);
  const { columns, rows } = floorShape(pages.length);
  // Resting colour of each tile; flashes are layered on top every frame.
  const base = useMemo(
    () =>
      pages.map((p, page) => {
        if (page === 0) return COLORS.header.clone().multiplyScalar(0.5);
        if (p.free) return COLORS.free.clone();
        const owner = p.owner ? OWNER_COLORS[p.owner.color % OWNER_COLORS.length]! : COLORS.free;
        const tint = owner.clone().multiplyScalar(p.cached ? 0.85 : 0.32);
        return p.dirty ? tint.lerp(COLORS.write, 0.55) : tint;
      }),
    [pages],
  );

  useEffect(() => () => {
    geometry.dispose();
    material.dispose();
  }, [geometry, material]);

  useLayoutEffect(() => {
    if (tiles.current && !tiles.current.instanceColor) tiles.current.setColorAt(0, COLORS.free);
    material.needsUpdate = true;
  }, [capacity, material]);

  useFrame(() => {
    const mesh = tiles.current;
    if (!mesh) return;
    const now = performance.now();
    const { shatter } = motion.current ?? { shatter: 0 };
    const dummy = scratch.object;
    const color = scratch.color;
    pages.forEach((_, page) => {
      const [x, y, z] = tilePosition(page, columns, rows);
      const flash = feed.pageFlash.get(page);
      const g = glow(flash, now, flashMs);
      let lift = 0;
      color.copy(base[page]!);
      if (g > 0 && flash) {
        const kind = flash.kind as keyof typeof FLASH;
        color.lerp(FLASH[kind] ?? COLORS.visit, g).multiplyScalar(1 + g * (kind === "evict" ? 0 : 2.2));
        lift = kind === "miss" ? g * 0.9 : kind === "evict" ? -g * 0.4 : g * 0.25;
      }
      // A crash drops the floor away a little, page by page.
      const fall = shatter * (0.6 + ((page * 7919) % 13) / 13) * 1.6;
      dummy.position.set(x, y + lift - fall, z);
      dummy.scale.set(1, 1 + g * 2, 1);
      dummy.updateMatrix();
      mesh.setMatrixAt(page, dummy.matrix);
      mesh.setColorAt(page, shatter > 0 ? color.lerp(COLORS.danger, shatter * 0.4) : color);
    });
    mesh.count = pages.length;
    mesh.instanceMatrix.needsUpdate = true;
    if (mesh.instanceColor) mesh.instanceColor.needsUpdate = true;
    mesh.boundingSphere = null;
  });

  return (
    <instancedMesh
      key={capacity}
      ref={tiles}
      args={[geometry, material, capacity]}
      frustumCulled={false}
      onPointerMove={(e) => {
        e.stopPropagation();
        onHover(e.instanceId ?? null, e);
      }}
      onPointerOut={() => onHover(null)}
    />
  );
});

const scratch = { object: new Object3D(), color: new Color() };
