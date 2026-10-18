// The B+tree as crystals joined by light. A query's descent sends a spark from each node to the
// child it visits next; a split flares magenta; a crash blows the crystals apart until recovery.
import { useFrame, type ThreeEvent } from "@react-three/fiber";
import { memo, useEffect, useLayoutEffect, useMemo, useRef, type RefObject } from "react";
import {
  BufferAttribute,
  BufferGeometry,
  Color,
  IcosahedronGeometry,
  InstancedMesh,
  LineBasicMaterial,
  MeshBasicMaterial,
  Object3D,
  OctahedronGeometry,
  Vector3,
} from "three";
import type { FeedState } from "../feed";
import { COLORS, CRYSTAL_DEPTH, glow, nextPowerOfTwo, type Motion, type Placement } from "./world";

const SPARKS = 96;

interface Spark {
  from: Vector3;
  to: Vector3;
  start: number;
}

interface Props {
  placement: Placement;
  feed: FeedState;
  flashMs: number;
  motion: RefObject<Motion>;
  onHover: (page: number | null, event?: ThreeEvent<PointerEvent>) => void;
}

export const CrystalTree = memo(function CrystalTree({ placement, feed, flashMs, motion, onHover }: Props) {
  const { nodes, edges } = placement;
  const capacity = nextPowerOfTwo(nodes.length);
  const crystals = useRef<InstancedMesh>(null);
  const facets = useRef<InstancedMesh>(null);
  const sparkMesh = useRef<InstancedMesh>(null);

  const geometry = useMemo(() => new OctahedronGeometry(0.5, 0), []);
  const body = useMemo(() => new MeshBasicMaterial({ transparent: true, opacity: 0.55, toneMapped: false, depthWrite: false }), []);
  const outline = useMemo(() => new MeshBasicMaterial({ wireframe: true, toneMapped: false }), []);
  const sparkGeometry = useMemo(() => new IcosahedronGeometry(0.11, 1), []);
  const sparkMaterial = useMemo(() => new MeshBasicMaterial({ toneMapped: false }), []);
  const lineMaterial = useMemo(() => new LineBasicMaterial({ vertexColors: true, toneMapped: false, transparent: true }), []);
  const lineGeometry = useMemo(() => {
    const g = new BufferGeometry();
    g.setAttribute("position", new BufferAttribute(new Float32Array(edges.length * 6), 3));
    g.setAttribute("color", new BufferAttribute(new Float32Array(edges.length * 6), 3));
    return g;
  }, [edges.length]);

  // Each crystal flies off in its own direction when the plug is pulled.
  const scatter = useMemo(
    () => nodes.map((_, i) => new Vector3(Math.sin(i * 12.9898) * 1.4, Math.cos(i * 78.233) * 0.9, Math.sin(i * 39.425))),
    [nodes],
  );
  const positions = useMemo(() => nodes.map((n) => new Vector3(n.x, n.y, n.z)), [nodes]);
  const sparks = useRef<Spark[]>([]);
  const seen = useRef(new Map<number, number>());

  useEffect(() => () => [geometry, body, outline, sparkGeometry, sparkMaterial, lineMaterial].forEach((x) => x.dispose()), [
    geometry, body, outline, sparkGeometry, sparkMaterial, lineMaterial,
  ]);
  useEffect(() => () => lineGeometry.dispose(), [lineGeometry]);

  // Per-instance colours must exist before the first draw, so the shaders are built to use them.
  useLayoutEffect(() => {
    for (const mesh of [crystals.current, facets.current, sparkMesh.current]) {
      if (mesh && !mesh.instanceColor) mesh.setColorAt(0, COLORS.edge);
    }
    body.needsUpdate = outline.needsUpdate = sparkMaterial.needsUpdate = true;
  }, [capacity, body, outline, sparkMaterial]);

  useFrame(({ clock }) => {
    const mesh = crystals.current;
    const wire = facets.current;
    const sparkInstances = sparkMesh.current;
    if (!mesh || !wire || !sparkInstances) return;
    const now = performance.now();
    const t = clock.elapsedTime;
    const { shatter, reduced } = motion.current ?? { shatter: 0, reduced: false };
    const dummy = scratch.object;
    const color = scratch.color;

    nodes.forEach((n, i) => {
      const flash = feed.nodeFlash.get(n.page);
      const g = glow(flash, now, flashMs);
      const away = scatter[i]!;
      const bob = reduced ? 0 : Math.sin(t * 0.9 + i * 0.7) * 0.04;
      const p = positions[i]!;
      p.set(n.x + away.x * shatter * 7, n.y + bob + away.y * shatter * 5, n.z + away.z * shatter * 7);
      dummy.position.copy(p);
      const sway = reduced ? 0 : Math.sin(t * 0.2 + i) * 0.08;
      dummy.rotation.set(away.x * shatter * 4, n.angle + sway, away.z * shatter * 4);
      const swell = 1 + g * 0.22;
      dummy.scale.set(n.length * swell, CRYSTAL_DEPTH * swell, CRYSTAL_DEPTH * swell);
      dummy.updateMatrix();
      mesh.setMatrixAt(i, dummy.matrix);
      wire.setMatrixAt(i, dummy.matrix);

      color.copy(n.leaf ? COLORS.leaf : COLORS.internal).multiplyScalar(0.55);
      if (g > 0) color.lerp(flash!.kind === "split" ? COLORS.split : COLORS.visit, g).multiplyScalar(1 + g * 2.4);
      if (shatter > 0) color.lerp(COLORS.danger, shatter * 0.7);
      mesh.setColorAt(i, color);
      wire.setColorAt(i, color.multiplyScalar(1.6));
    });
    mesh.count = wire.count = nodes.length;
    for (const m of [mesh, wire]) {
      m.instanceMatrix.needsUpdate = true;
      if (m.instanceColor) m.instanceColor.needsUpdate = true;
      m.boundingSphere = null; // recomputed on the next raycast, since crystals move
    }

    // Light along every pointer, brighter where the descent just passed.
    const linePositions = lineGeometry.getAttribute("position") as BufferAttribute;
    const lineColors = lineGeometry.getAttribute("color") as BufferAttribute;
    edges.forEach(([from, to], e) => {
      const a = positions[from]!;
      const b = positions[to]!;
      linePositions.setXYZ(e * 2, a.x, a.y - CRYSTAL_DEPTH / 2, a.z);
      linePositions.setXYZ(e * 2 + 1, b.x, b.y + CRYSTAL_DEPTH / 2, b.z);
      const flash = feed.nodeFlash.get(nodes[to]!.page);
      const lit = flash?.kind === "visit" ? glow(flash, now, flashMs) : 0;
      color.copy(COLORS.edge).lerp(COLORS.edgeLit, lit).multiplyScalar(1 + lit * 2);
      lineColors.setXYZ(e * 2, color.r, color.g, color.b);
      lineColors.setXYZ(e * 2 + 1, color.r, color.g, color.b);
    });
    linePositions.needsUpdate = true;
    lineColors.needsUpdate = true;
    lineMaterial.opacity = 1 - shatter * 0.85;

    // New visits launch sparks from the parent (or from above the root) down to the node.
    for (const [page, flash] of feed.nodeFlash) {
      if (flash.kind !== "visit" || flash.at <= (seen.current.get(page) ?? 0)) continue;
      seen.current.set(page, flash.at);
      const target = placement.index.get(page);
      if (target === undefined || reduced || shatter > 0.05) continue;
      const parent = placement.parent.get(page);
      const from = parent !== undefined ? positions[placement.index.get(parent)!]! : positions[target]!.clone().setY(positions[target]!.y + 2.5);
      if (sparks.current.length >= SPARKS) sparks.current.shift();
      sparks.current.push({ from: from.clone(), to: positions[target]!.clone(), start: flash.at });
    }
    const travel = flashMs * 0.55;
    sparks.current = sparks.current.filter((s) => now - s.start < travel);
    sparks.current.forEach((s, i) => {
      const k = Math.min(1, (now - s.start) / travel);
      const eased = k * k * (3 - 2 * k);
      dummy.position.lerpVectors(s.from, s.to, eased);
      dummy.rotation.set(0, 0, 0);
      dummy.scale.setScalar(1.4 - k * 0.6);
      dummy.updateMatrix();
      sparkInstances.setMatrixAt(i, dummy.matrix);
      sparkInstances.setColorAt(i, color.copy(COLORS.spark).multiplyScalar(3.2));
    });
    sparkInstances.count = sparks.current.length;
    sparkInstances.instanceMatrix.needsUpdate = true;
    if (sparkInstances.instanceColor) sparkInstances.instanceColor.needsUpdate = true;
  });

  const hover = (e: ThreeEvent<PointerEvent>) => {
    e.stopPropagation();
    const node = e.instanceId !== undefined ? nodes[e.instanceId] : undefined;
    onHover(node ? node.page : null, e);
  };

  return (
    <group>
      <instancedMesh
        key={`body-${capacity}`}
        ref={crystals}
        args={[geometry, body, capacity]}
        frustumCulled={false}
        onPointerMove={hover}
        onPointerOut={() => onHover(null)}
      />
      <instancedMesh key={`facets-${capacity}`} ref={facets} args={[geometry, outline, capacity]} frustumCulled={false} raycast={noRaycast} />
      <lineSegments args={[lineGeometry, lineMaterial]} frustumCulled={false} raycast={noRaycast} />
      <instancedMesh ref={sparkMesh} args={[sparkGeometry, sparkMaterial, SPARKS]} frustumCulled={false} raycast={noRaycast} />
    </group>
  );
});

const scratch = { object: new Object3D(), color: new Color() };

function noRaycast() {
  // Decorations are not hoverable; only the crystal bodies are.
}
