// The data file as a platform of tiles under the tree, one rounded tile per page, coloured by the tree
// that owns it. Pages in the buffer pool sit raised and lit; a disk read lifts a tile and burns amber,
// a cache hit flashes green, a write red, and an eviction lets the tile sink back into the dark.
import { useFrame, type ThreeEvent } from "@react-three/fiber";
import { memo, useEffect, useLayoutEffect, useMemo, useRef, type RefObject } from "react";
import {
  AdditiveBlending,
  CanvasTexture,
  Color,
  InstancedMesh,
  MeshBasicMaterial,
  MeshPhysicalMaterial,
  MeshStandardMaterial,
  Object3D,
  PlaneGeometry,
  SRGBColorSpace,
} from "three";
import { RoundedBoxGeometry } from "three/addons/geometries/RoundedBoxGeometry.js";
import type { FeedState } from "../feed";
import { COLORS, FLOOR_Y, OWNER_COLORS, TILE_GAP, floorShape, glow, nextPowerOfTwo, tilePosition, type Motion } from "./world";

export interface PageInfo {
  owner?: { color: number };
  cached: boolean;
  dirty: boolean;
  free: boolean;
}

interface Props {
  pages: PageInfo[];
  pageSize: number;
  feed: FeedState;
  flashMs: number;
  motion: RefObject<Motion>;
  onHover: (page: number | null, event?: ThreeEvent<PointerEvent>) => void;
}

const FLASH = { hit: COLORS.hit, miss: COLORS.miss, write: COLORS.write, evict: COLORS.evict } as const;
const TILE = 0.26;
const RAISED = 0.06; // how far a cached page stands above the rest

function plaque(pages: number, pageSize: number): CanvasTexture {
  const canvas = document.createElement("canvas");
  canvas.width = 1024;
  canvas.height = 96;
  const ctx = canvas.getContext("2d")!;
  ctx.font = '700 44px "Inter Variable", system-ui, sans-serif';
  ctx.textBaseline = "middle";
  ctx.fillStyle = "rgba(200,210,255,0.92)";
  ctx.fillText("DATA FILE", 8, 48);
  const width = ctx.measureText("DATA FILE").width;
  ctx.font = '500 38px "JetBrains Mono Variable", ui-monospace, monospace';
  ctx.fillStyle = "rgba(160,172,215,0.75)";
  ctx.fillText(`  ${pages.toLocaleString()} pages × ${pageSize / 1024} KB`, 8 + width, 50);
  const texture = new CanvasTexture(canvas);
  texture.colorSpace = SRGBColorSpace;
  return texture;
}

export const PageFloor = memo(function PageFloor({ pages, pageSize, feed, flashMs, motion, onHover }: Props) {
  const capacity = nextPowerOfTwo(pages.length);
  const tiles = useRef<InstancedMesh>(null);
  const glows = useRef<InstancedMesh>(null);
  const { columns, rows } = floorShape(pages.length);
  const width = columns * TILE_GAP + 0.5;
  const depth = rows * TILE_GAP + 0.5;
  const centreZ = tilePosition(0, columns, rows)[2] + ((rows - 1) * TILE_GAP) / 2;

  const tileGeometry = useMemo(() => new RoundedBoxGeometry(TILE, 0.08, TILE, 2, 0.035), []);
  const tileMaterial = useMemo(() => new MeshStandardMaterial({ roughness: 0.35, metalness: 0.3 }), []);
  const glowMaterial = useMemo(() => new MeshBasicMaterial({ toneMapped: false, blending: AdditiveBlending, transparent: true, depthWrite: false }), []);
  const base = useMemo(
    () => ({
      geometry: new RoundedBoxGeometry(1, 1, 1, 3, 0.06),
      material: new MeshPhysicalMaterial({ color: COLORS.platform, roughness: 0.25, metalness: 0.4, clearcoat: 1, clearcoatRoughness: 0.2, emissive: new Color("#1b2a6b"), emissiveIntensity: 0.25 }),
    }),
    [],
  );
  const label = useMemo(() => {
    const map = plaque(pages.length, pageSize);
    return { map, geometry: new PlaneGeometry(4, 0.375), material: new MeshBasicMaterial({ map, transparent: true, toneMapped: false, depthWrite: false }) };
  }, [pages.length, pageSize]);

  // Resting colour of each tile; flashes are layered on top every frame.
  const rest = useMemo(
    () =>
      pages.map((p, page) => {
        if (page === 0) return COLORS.header.clone().multiplyScalar(0.55);
        if (p.free) return COLORS.free.clone();
        const owner = p.owner ? OWNER_COLORS[p.owner.color % OWNER_COLORS.length]! : COLORS.free;
        const tint = owner.clone().multiplyScalar(p.cached ? 0.95 : 0.28);
        return p.dirty ? tint.lerp(COLORS.write, 0.55) : tint;
      }),
    [pages],
  );

  useEffect(
    () => () => {
      [tileGeometry, tileMaterial, glowMaterial, base.geometry, base.material].forEach((x) => x.dispose());
    },
    [tileGeometry, tileMaterial, glowMaterial, base],
  );
  useEffect(
    () => () => {
      label.map.dispose();
      label.geometry.dispose();
      label.material.dispose();
    },
    [label],
  );

  useLayoutEffect(() => {
    for (const mesh of [tiles.current, glows.current]) if (mesh && !mesh.instanceColor) mesh.setColorAt(0, COLORS.free);
    tileMaterial.needsUpdate = glowMaterial.needsUpdate = true;
  }, [capacity, tileMaterial, glowMaterial]);

  useFrame(() => {
    const mesh = tiles.current;
    const halo = glows.current;
    if (!mesh || !halo) return;
    const now = performance.now();
    const { shatter } = motion.current ?? { shatter: 0 };
    const dummy = scratch.object;
    const color = scratch.color;
    pages.forEach((info, page) => {
      const [x, , z] = tilePosition(page, columns, rows);
      const flash = feed.pageFlash.get(page);
      const g = glow(flash, now, flashMs);
      let lift = info.cached ? RAISED : 0;
      color.set(0, 0, 0);
      if (info.cached) color.copy(rest[page]!).multiplyScalar(0.35); // a faint glow while in memory
      if (g > 0 && flash) {
        const kind = flash.kind as keyof typeof FLASH;
        color.lerp(FLASH[kind] ?? COLORS.visit, g).multiplyScalar(kind === "evict" ? 0.2 : 1 + g * 2.5);
        lift += kind === "miss" ? g * 0.7 : kind === "evict" ? -g * 0.15 : g * 0.25;
      }
      // A crash drops the floor away a little, page by page.
      const fall = shatter * (0.6 + ((page * 7919) % 13) / 13) * 1.6;
      dummy.position.set(x, FLOOR_Y + 0.12 + lift - fall, z);
      dummy.scale.set(1, 1, 1);
      dummy.updateMatrix();
      mesh.setMatrixAt(page, dummy.matrix);
      mesh.setColorAt(page, shatter > 0 ? scratch.tint.copy(rest[page]!).lerp(COLORS.danger, shatter * 0.4) : rest[page]!);
      dummy.scale.set(1.08, 1.4, 1.08);
      dummy.updateMatrix();
      halo.setMatrixAt(page, dummy.matrix);
      halo.setColorAt(page, color);
    });
    for (const m of [mesh, halo]) {
      m.count = pages.length;
      m.instanceMatrix.needsUpdate = true;
      if (m.instanceColor) m.instanceColor.needsUpdate = true;
    }
    mesh.boundingSphere = null;
  });

  return (
    <group>
      <mesh geometry={base.geometry} material={base.material} position={[0, FLOOR_Y, centreZ]} scale={[width, 0.14, depth]} raycast={() => undefined} />
      <mesh
        geometry={label.geometry}
        material={label.material}
        position={[-width / 2 + 2.05, FLOOR_Y + 0.01, centreZ + depth / 2 + 0.3]}
        rotation={[-Math.PI / 2, 0, 0]}
        raycast={() => undefined}
      />
      <instancedMesh
        key={`tiles-${capacity}`}
        ref={tiles}
        args={[tileGeometry, tileMaterial, capacity]}
        frustumCulled={false}
        onPointerMove={(e) => {
          e.stopPropagation();
          onHover(e.instanceId ?? null, e);
        }}
        onPointerOut={() => onHover(null)}
      />
      <instancedMesh key={`glow-${capacity}`} ref={glows} args={[tileGeometry, glowMaterial, capacity]} frustumCulled={false} raycast={() => undefined} />
    </group>
  );
});

const scratch = { object: new Object3D(), color: new Color(), tint: new Color() };
