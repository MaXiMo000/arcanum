// The B+tree as lit glass slabs with their keys written on their faces, hung from each other by curved
// pointers that leave each parent from the gap between the keys they separate. The leaves are chained
// left to right. A query's descent sends a comet down each pointer it follows and rings the node it
// reaches; a split flares magenta; a crash blows the slabs apart until recovery.
import { useFrame, useThree, type ThreeEvent } from "@react-three/fiber";
import { memo, useEffect, useLayoutEffect, useMemo, useRef, type RefObject } from "react";
import {
  AdditiveBlending,
  CanvasTexture,
  Color,
  DoubleSide,
  Group,
  IcosahedronGeometry,
  InstancedMesh,
  type InterleavedBufferAttribute,
  MeshBasicMaterial,
  MeshPhysicalMaterial,
  Object3D,
  PlaneGeometry,
  SRGBColorSpace,
  TorusGeometry,
  Vector3,
} from "three";
import { RoundedBoxGeometry } from "three/addons/geometries/RoundedBoxGeometry.js";
import { LineMaterial } from "three/addons/lines/LineMaterial.js";
import { LineSegments2 } from "three/addons/lines/LineSegments2.js";
import { LineSegmentsGeometry } from "three/addons/lines/LineSegmentsGeometry.js";
import type { FeedState } from "../feed";
import { COLORS, SLAB_DEPTH, SLAB_HEIGHT, glow, type CrystalNode, type Motion, type Placement } from "./world";

const SEGMENTS = 18; // points per curved pointer
const COMETS = 64;
const TRAIL = 6; // glowing beads behind each comet's head
const RINGS = 48;
const MAX_LABELS = 400; // beyond this, slabs stay unlabelled (hover still shows the keys)
const LABEL_PX = 64; // texture pixels per world unit of slab length

interface Comet {
  edge: number;
  start: number;
}

interface Ring {
  node: number;
  start: number;
  split: boolean;
}

interface Props {
  placement: Placement;
  feed: FeedState;
  flashMs: number;
  motion: RefObject<Motion>;
  onHover: (page: number | null, event?: ThreeEvent<PointerEvent>) => void;
}

/** A slab's face: its keys in cells divided by thin rules, drawn once per distinct text and length. */
function labelTexture(node: CrystalNode): CanvasTexture {
  const width = Math.max(64, Math.round(node.length * LABEL_PX * 2));
  const height = Math.round(SLAB_HEIGHT * LABEL_PX * 2);
  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  const ctx = canvas.getContext("2d")!;
  const keys = node.labels.length ? node.labels : ["empty"];
  const cell = width / keys.length;
  ctx.font = `600 ${Math.round(height * 0.42)}px "JetBrains Mono Variable", ui-monospace, monospace`;
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  keys.forEach((key, i) => {
    if (i > 0) {
      ctx.fillStyle = "rgba(255,255,255,0.22)";
      ctx.fillRect(Math.round(i * cell) - 1, height * 0.22, 2, height * 0.56);
    }
    ctx.fillStyle = node.labels.length ? "rgba(255,255,255,0.96)" : "rgba(255,255,255,0.45)";
    const text = ctx.measureText(key).width > cell * 0.9 ? `${key.slice(0, Math.max(1, Math.floor(key.length * (cell * 0.9) / ctx.measureText(key).width) - 1))}…` : key;
    ctx.fillText(text, (i + 0.5) * cell, height / 2 + 1);
  });
  const texture = new CanvasTexture(canvas);
  texture.colorSpace = SRGBColorSpace;
  texture.anisotropy = 4;
  return texture;
}

export const CrystalTree = memo(function CrystalTree({ placement, feed, flashMs, motion, onHover }: Props) {
  const { nodes, edges, links } = placement;
  const { size } = useThree();
  const groups = useRef<(Group | null)[]>([]);
  const cometMesh = useRef<InstancedMesh>(null);
  const ringMesh = useRef<InstancedMesh>(null);
  const hovered = useRef<number | null>(null);

  // One rounded geometry per slab length (rounded), shared by slabs of the same size.
  const geometries = useMemo(() => {
    const cache = new Map<number, RoundedBoxGeometry>();
    return nodes.map((n) => {
      const length = Math.round(n.length * 20) / 20;
      let g = cache.get(length);
      if (!g) {
        g = new RoundedBoxGeometry(length, SLAB_HEIGHT, SLAB_DEPTH, 3, 0.1);
        cache.set(length, g);
      }
      return g;
    });
  }, [nodes]);
  const materials = useMemo(
    () =>
      nodes.map(
        (n) =>
          new MeshPhysicalMaterial({
            color: n.leaf ? COLORS.leaf : COLORS.internal,
            emissive: n.leaf ? COLORS.leafGlow : COLORS.internalGlow,
            emissiveIntensity: 0.35,
            roughness: 0.22,
            metalness: 0.25,
            clearcoat: 1,
            clearcoatRoughness: 0.15,
          }),
      ),
    [nodes],
  );
  const labels = useMemo(
    () =>
      nodes.slice(0, MAX_LABELS).map((n) => ({
        geometry: new PlaneGeometry(n.length * 0.94, SLAB_HEIGHT * 0.86),
        material: new MeshBasicMaterial({ map: labelTexture(n), transparent: true, depthWrite: false, toneMapped: false }),
      })),
    [nodes],
  );

  // Pointers (fat lines along bezier curves) and the leaf chain, rebuilt each frame as slabs move.
  const pointer = useMemo(() => {
    const geometry = new LineSegmentsGeometry();
    const material = new LineMaterial({ linewidth: 2.4, vertexColors: true, transparent: true, toneMapped: false });
    return { geometry, material, line: new LineSegments2(geometry, material) };
  }, []);
  const chain = useMemo(() => {
    const geometry = new LineSegmentsGeometry();
    const material = new LineMaterial({ linewidth: 1.4, color: COLORS.link, dashed: true, dashSize: 0.12, gapSize: 0.1, transparent: true, opacity: 0.8, toneMapped: false });
    return { geometry, material, line: new LineSegments2(geometry, material) };
  }, []);
  // The line geometries keep these arrays (setPositions does not copy), so each frame writes into them
  // and flags them for upload instead of allocating new buffers.
  const buffers = useMemo(() => {
    const pointerPositions = new Float32Array(Math.max(1, edges.length * SEGMENTS) * 6);
    const pointerColors = new Float32Array(pointerPositions.length);
    const chainPositions = new Float32Array(Math.max(1, links.length) * 6);
    pointer.geometry.setPositions(pointerPositions);
    pointer.geometry.setColors(pointerColors);
    chain.geometry.setPositions(chainPositions);
    return {
      pointerPositions,
      pointerColors,
      chainPositions,
      chainMeasured: false,
      curves: edges.map(() => Array.from({ length: SEGMENTS + 1 }, () => new Vector3())),
    };
  }, [edges, links.length, pointer, chain]);

  const cometGeometry = useMemo(() => new IcosahedronGeometry(0.09, 2), []);
  const cometMaterial = useMemo(() => new MeshBasicMaterial({ toneMapped: false, blending: AdditiveBlending, depthWrite: false, transparent: true }), []);
  const ringGeometry = useMemo(() => new TorusGeometry(1, 0.025, 8, 64), []);
  const ringMaterial = useMemo(
    () => new MeshBasicMaterial({ toneMapped: false, blending: AdditiveBlending, depthWrite: false, transparent: true, side: DoubleSide }),
    [],
  );

  const scatter = useMemo(
    () => nodes.map((_, i) => new Vector3(Math.sin(i * 12.9898) * 1.4, Math.cos(i * 78.233) * 0.9, Math.sin(i * 39.425))),
    [nodes],
  );
  const positions = useMemo(() => nodes.map((n) => new Vector3(n.x, n.y, n.z)), [nodes]);
  const comets = useRef<Comet[]>([]);
  const rings = useRef<Ring[]>([]);
  const seen = useRef(new Map<number, number>());
  const edgeTo = useMemo(() => new Map(edges.map((e, i) => [e.to, i])), [edges]);

  useEffect(
    () => () => {
      new Set(geometries).forEach((g) => g.dispose());
      materials.forEach((m) => m.dispose());
      labels.forEach((l) => {
        l.geometry.dispose();
        l.material.map?.dispose();
        l.material.dispose();
      });
    },
    [geometries, materials, labels],
  );
  useEffect(
    () => () => {
      for (const x of [pointer, chain]) {
        x.geometry.dispose();
        x.material.dispose();
      }
      [cometGeometry, cometMaterial, ringGeometry, ringMaterial].forEach((x) => x.dispose());
    },
    [pointer, chain, cometGeometry, cometMaterial, ringGeometry, ringMaterial],
  );
  // Per-instance colours must exist before the first draw, so the shaders are built to use them.
  useLayoutEffect(() => {
    for (const mesh of [cometMesh.current, ringMesh.current]) if (mesh && !mesh.instanceColor) mesh.setColorAt(0, COLORS.spark);
    cometMaterial.needsUpdate = ringMaterial.needsUpdate = true;
  }, [cometMaterial, ringMaterial]);

  useEffect(() => {
    pointer.material.resolution.set(size.width, size.height);
    chain.material.resolution.set(size.width, size.height);
  }, [pointer, chain, size.width, size.height]);

  useFrame(({ clock }) => {
    const now = performance.now();
    const t = clock.elapsedTime;
    const { shatter, reduced } = motion.current ?? { shatter: 0, reduced: false };
    const color = scratch.color;

    // Slabs: drift, swell when visited, flare when split, scatter on a crash.
    nodes.forEach((n, i) => {
      const group = groups.current[i];
      const material = materials[i]!;
      if (!group) return;
      const flash = feed.nodeFlash.get(n.page);
      const g = glow(flash, now, flashMs);
      const away = scatter[i]!;
      const bob = reduced ? 0 : Math.sin(t * 0.8 + i * 0.7) * 0.05;
      const p = positions[i]!;
      p.set(n.x + away.x * shatter * 7, n.y + bob + away.y * shatter * 5, n.z + away.z * shatter * 7);
      group.position.copy(p);
      group.rotation.set(away.x * shatter * 3, n.angle, away.z * shatter * 3);
      const lift = hovered.current === i ? 1.06 : 1;
      group.scale.setScalar((1 + g * 0.16) * lift);

      const rest = n.leaf ? COLORS.leafGlow : COLORS.internalGlow;
      color.copy(rest);
      let intensity = hovered.current === i ? 0.9 : 0.35;
      if (g > 0) {
        color.lerp(flash!.kind === "split" ? COLORS.split : COLORS.visit, Math.min(1, g * 1.4));
        intensity += g * 1.3;
      }
      if (shatter > 0) {
        color.lerp(COLORS.danger, shatter * 0.8);
        intensity += shatter * 1.5;
      }
      material.emissive.copy(color);
      material.emissiveIntensity = intensity;
    });

    // Pointers: a bezier from the parent's key gap down to the child's top, lit where the descent passed.
    const pos = buffers.pointerPositions;
    const col = buffers.pointerColors;
    edges.forEach((e, k) => {
      const parent = nodes[e.from]!;
      const a = positions[e.from]!;
      const b = positions[e.to]!;
      const ax = a.x + Math.cos(parent.angle) * e.offset;
      const az = a.z - Math.sin(parent.angle) * e.offset;
      const ay = a.y - SLAB_HEIGHT / 2;
      const by = b.y + SLAB_HEIGHT / 2;
      const flash = feed.nodeFlash.get(nodes[e.to]!.page);
      const lit = flash?.kind === "visit" ? glow(flash, now, flashMs) : 0;
      color.copy(COLORS.edge).lerp(COLORS.edgeLit, lit).multiplyScalar(1 + lit * 0.9);
      const curve = buffers.curves[k]!;
      for (let s = 0; s <= SEGMENTS; s++) {
        const u = s / SEGMENTS;
        // Cubic bezier: straight down out of the parent, straight down into the child.
        const mt = 1 - u;
        const w0 = mt * mt * mt;
        const w1 = 3 * mt * mt * u;
        const w2 = 3 * mt * u * u;
        const w3 = u * u * u;
        const midY = (ay + by) / 2;
        curve[s]!.set(
          (w0 + w1) * ax + (w2 + w3) * b.x,
          w0 * ay + w1 * midY + w2 * midY + w3 * by,
          (w0 + w1) * az + (w2 + w3) * b.z,
        );
      }
      for (let s = 0; s < SEGMENTS; s++) {
        const o = (k * SEGMENTS + s) * 6;
        const p0 = curve[s]!;
        const p1 = curve[s + 1]!;
        pos[o] = p0.x;
        pos[o + 1] = p0.y;
        pos[o + 2] = p0.z;
        pos[o + 3] = p1.x;
        pos[o + 4] = p1.y;
        pos[o + 5] = p1.z;
        col[o] = col[o + 3] = color.r;
        col[o + 1] = col[o + 4] = color.g;
        col[o + 2] = col[o + 5] = color.b;
      }
    });
    (pointer.geometry.attributes.instanceStart as InterleavedBufferAttribute).data.needsUpdate = true;
    (pointer.geometry.attributes.instanceColorStart as InterleavedBufferAttribute).data.needsUpdate = true;
    pointer.material.opacity = 1 - shatter * 0.85;

    const chainPos = buffers.chainPositions;
    links.forEach(([from, to], k) => {
      const a = positions[from]!;
      const b = positions[to]!;
      const o = k * 6;
      chainPos[o] = a.x + nodes[from]!.length / 2;
      chainPos[o + 1] = a.y;
      chainPos[o + 2] = a.z;
      chainPos[o + 3] = b.x - nodes[to]!.length / 2;
      chainPos[o + 4] = b.y;
      chainPos[o + 5] = b.z;
    });
    (chain.geometry.attributes.instanceStart as InterleavedBufferAttribute).data.needsUpdate = true;
    if (links.length && !buffers.chainMeasured) {
      chain.line.computeLineDistances(); // dash spacing; the slabs barely move after this
      buffers.chainMeasured = true;
    }
    chain.material.opacity = 0.8 * (1 - shatter);

    // New visits launch a comet down the pointer into the node, and every visit or split rings it.
    for (const [page, flash] of feed.nodeFlash) {
      if (flash.at <= (seen.current.get(page) ?? 0)) continue;
      seen.current.set(page, flash.at);
      const target = placement.index.get(page);
      if (target === undefined || reduced || shatter > 0.05) continue;
      if (rings.current.length >= RINGS) rings.current.shift();
      rings.current.push({ node: target, start: flash.at, split: flash.kind === "split" });
      const edge = edgeTo.get(target);
      if (flash.kind === "visit" && edge !== undefined) {
        if (comets.current.length >= COMETS) comets.current.shift();
        comets.current.push({ edge, start: flash.at });
      }
    }

    const dummy = scratch.object;
    const travel = flashMs * 0.6;
    const cometInstances = cometMesh.current;
    if (cometInstances) {
      comets.current = comets.current.filter((c) => now - c.start < travel);
      let n = 0;
      for (const c of comets.current) {
        const curve = buffers.curves[c.edge];
        if (!curve) continue;
        const head = Math.min(1, (now - c.start) / travel);
        for (let j = 0; j < TRAIL; j++) {
          const u = head - j * 0.05;
          if (u < 0) break;
          const f = u * SEGMENTS;
          const s = Math.min(SEGMENTS - 1, Math.floor(f));
          dummy.position.lerpVectors(curve[s]!, curve[s + 1]!, f - s);
          dummy.rotation.set(0, 0, 0);
          dummy.scale.setScalar((1.6 - j * 0.22) * (1 - head * 0.3));
          dummy.updateMatrix();
          cometInstances.setMatrixAt(n, dummy.matrix);
          cometInstances.setColorAt(n, color.copy(COLORS.spark).multiplyScalar(1.7 * (1 - j / TRAIL)));
          n++;
        }
      }
      cometInstances.count = n;
      cometInstances.instanceMatrix.needsUpdate = true;
      if (cometInstances.instanceColor) cometInstances.instanceColor.needsUpdate = true;
    }

    const ringInstances = ringMesh.current;
    if (ringInstances) {
      const life = flashMs * 1.1;
      rings.current = rings.current.filter((r) => now - r.start < life);
      rings.current.forEach((r, k) => {
        const age = (now - r.start) / life;
        const n = nodes[r.node]!;
        dummy.position.copy(positions[r.node]!);
        dummy.rotation.set(Math.PI / 2, 0, n.angle);
        const radius = n.length * 0.55 + age * 0.9;
        dummy.scale.set(radius, radius * 0.5 + 0.2, radius);
        dummy.updateMatrix();
        ringInstances.setMatrixAt(k, dummy.matrix);
        ringInstances.setColorAt(k, color.copy(r.split ? COLORS.split : COLORS.visit).multiplyScalar(1.3 * (1 - age)));
      });
      ringInstances.count = rings.current.length;
      ringInstances.instanceMatrix.needsUpdate = true;
      if (ringInstances.instanceColor) ringInstances.instanceColor.needsUpdate = true;
    }
  });

  const hover = (i: number) => (e: ThreeEvent<PointerEvent>) => {
    e.stopPropagation();
    hovered.current = i;
    onHover(nodes[i]!.page, e);
  };
  const leave = () => {
    hovered.current = null;
    onHover(null);
  };

  return (
    <group>
      {nodes.map((n, i) => (
        <group key={n.page} ref={(g) => void (groups.current[i] = g)}>
          <mesh
            geometry={geometries[i]}
            material={materials[i]}
            castShadow
            onPointerMove={hover(i)}
            onPointerOut={leave}
          />
          {labels[i] && (
            <mesh geometry={labels[i]!.geometry} material={labels[i]!.material} position={[0, 0, SLAB_DEPTH / 2 + 0.006]} raycast={noRaycast} />
          )}
          {labels[i] && (
            <mesh
              geometry={labels[i]!.geometry}
              material={labels[i]!.material}
              position={[0, 0, -SLAB_DEPTH / 2 - 0.006]}
              rotation={[0, Math.PI, 0]}
              raycast={noRaycast}
            />
          )}
        </group>
      ))}
      <primitive object={pointer.line} raycast={noRaycast} frustumCulled={false} />
      <primitive object={chain.line} raycast={noRaycast} frustumCulled={false} />
      <instancedMesh ref={cometMesh} args={[cometGeometry, cometMaterial, COMETS * TRAIL]} frustumCulled={false} raycast={noRaycast} />
      <instancedMesh ref={ringMesh} args={[ringGeometry, ringMaterial, RINGS]} frustumCulled={false} raycast={noRaycast} />
    </group>
  );
});

const scratch = { object: new Object3D(), color: new Color() };

function noRaycast() {
  // Decorations are not hoverable; only the slab bodies are.
}
