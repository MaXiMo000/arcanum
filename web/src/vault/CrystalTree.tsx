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
  IcosahedronGeometry,
  InstancedBufferAttribute,
  InstancedMesh,
  type InterleavedBufferAttribute,
  Matrix4,
  MeshBasicMaterial,
  MeshPhysicalMaterial,
  Object3D,
  PlaneGeometry,
  SRGBColorSpace,
  ShaderMaterial,
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
const ATLAS_WIDTH = 4096;
const CELL_WIDTH = 512; // one label per cell, its width trimmed to the slab face's proportions
const CELL_HEIGHT = 52;
const FACE_WIDTH = 0.94; // of the slab's length
const FACE_HEIGHT = 0.86; // of the slab's height

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

/**
 * Every slab's face in one texture: each label in its own cell, the keys divided by thin rules. Returns
 * the texture and, per label, the [u, v, width, height] of its cell, for the label shader.
 */
function labelAtlas(nodes: CrystalNode[]): { texture: CanvasTexture; rects: Float32Array } {
  const columns = ATLAS_WIDTH / CELL_WIDTH;
  const rows = Math.max(1, Math.ceil(nodes.length / columns));
  const canvas = document.createElement("canvas");
  canvas.width = ATLAS_WIDTH;
  canvas.height = rows * CELL_HEIGHT;
  const ctx = canvas.getContext("2d")!;
  ctx.font = `600 ${Math.round(CELL_HEIGHT * 0.46)}px "JetBrains Mono Variable", ui-monospace, monospace`;
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  const rects = new Float32Array(nodes.length * 4);
  nodes.forEach((node, i) => {
    const x0 = (i % columns) * CELL_WIDTH;
    const y0 = Math.floor(i / columns) * CELL_HEIGHT;
    // Trim the cell to the face's proportions so the text is never stretched.
    const aspect = (node.length * FACE_WIDTH) / (SLAB_HEIGHT * FACE_HEIGHT);
    const width = Math.min(CELL_WIDTH, Math.round(CELL_HEIGHT * aspect));
    const keys = node.labels.length ? node.labels : ["empty"];
    const cell = width / keys.length;
    keys.forEach((key, k) => {
      if (k > 0) {
        ctx.fillStyle = "rgba(255,255,255,0.22)";
        ctx.fillRect(x0 + Math.round(k * cell) - 1, y0 + CELL_HEIGHT * 0.22, 2, CELL_HEIGHT * 0.56);
      }
      ctx.fillStyle = node.labels.length ? "rgba(255,255,255,0.96)" : "rgba(255,255,255,0.45)";
      const measured = ctx.measureText(key).width;
      const text = measured > cell * 0.9 ? `${key.slice(0, Math.max(1, Math.floor((key.length * cell * 0.9) / measured) - 1))}…` : key;
      ctx.fillText(text, x0 + (k + 0.5) * cell, y0 + CELL_HEIGHT / 2 + 1);
    });
    // The texture is flipped vertically on upload, so v counts from the bottom of the canvas.
    rects.set([x0 / canvas.width, 1 - (y0 + CELL_HEIGHT) / canvas.height, width / canvas.width, CELL_HEIGHT / canvas.height], i * 4);
  });
  const texture = new CanvasTexture(canvas);
  texture.colorSpace = SRGBColorSpace;
  texture.anisotropy = 4;
  return { texture, rects };
}

/** Draws each label instance's own cell of the atlas onto a unit quad. */
function labelMaterial(texture: CanvasTexture) {
  return new ShaderMaterial({
    uniforms: { map: { value: texture }, opacity: { value: 1 } },
    vertexShader: `
      attribute vec4 uvRect;
      varying vec2 vUv;
      void main() {
        vUv = uvRect.xy + uv * uvRect.zw;
        gl_Position = projectionMatrix * modelViewMatrix * instanceMatrix * vec4(position, 1.0);
      }`,
    fragmentShader: `
      uniform sampler2D map;
      uniform float opacity;
      varying vec2 vUv;
      void main() {
        vec4 c = texture2D(map, vUv);
        if (c.a < 0.04) discard;
        gl_FragColor = vec4(c.rgb, c.a * opacity);
      }`,
    transparent: true,
    depthWrite: false,
    toneMapped: false,
  });
}

export const CrystalTree = memo(function CrystalTree({ placement, feed, flashMs, motion, onHover }: Props) {
  const { nodes, edges, links } = placement;
  const { size } = useThree();
  const bodies = useRef<(InstancedMesh | null)[]>([]);
  const glows = useRef<(InstancedMesh | null)[]>([]);
  const labelMesh = useRef<InstancedMesh>(null);
  const cometMesh = useRef<InstancedMesh>(null);
  const ringMesh = useRef<InstancedMesh>(null);
  const hovered = useRef<number | null>(null);

  // Slabs of the same length share one rounded geometry and one instanced draw (and one more for their
  // flash glow), so the tree costs a handful of draw calls however many nodes it has.
  const buckets = useMemo(() => {
    const byLength = new Map<number, number[]>();
    nodes.forEach((n, i) => {
      const length = Math.round(n.length * 20) / 20;
      byLength.set(length, [...(byLength.get(length) ?? []), i]);
    });
    return [...byLength].map(([length, members]) => ({
      members,
      geometry: new RoundedBoxGeometry(length, SLAB_HEIGHT, SLAB_DEPTH, 3, 0.1),
    }));
  }, [nodes]);
  const place = useMemo(() => {
    const at = new Array<[number, number]>(nodes.length);
    buckets.forEach((b, i) => b.members.forEach((node, k) => (at[node] = [i, k])));
    return at;
  }, [buckets, nodes.length]);
  const bodyMaterial = useMemo(
    () =>
      new MeshPhysicalMaterial({
        roughness: 0.22,
        metalness: 0.25,
        clearcoat: 1,
        clearcoatRoughness: 0.15,
        emissive: new Color("#0b1030"),
      }),
    [],
  );
  const glowMaterial = useMemo(
    () => new MeshBasicMaterial({ toneMapped: false, blending: AdditiveBlending, transparent: true, depthWrite: false }),
    [],
  );
  // Labels: one quad per face (front and back), each showing its own cell of a shared atlas.
  const labels = useMemo(() => {
    const labelled = nodes.slice(0, MAX_LABELS);
    const { texture, rects } = labelAtlas(labelled);
    const geometry = new PlaneGeometry(1, 1);
    const both = new Float32Array(rects.length * 2);
    both.set(rects);
    both.set(rects, rects.length);
    geometry.setAttribute("uvRect", new InstancedBufferAttribute(both, 4));
    const faces = labelled.map((n) => {
      const scale = new Matrix4().makeScale(n.length * FACE_WIDTH, SLAB_HEIGHT * FACE_HEIGHT, 1);
      const front = new Matrix4().makeTranslation(0, 0, SLAB_DEPTH / 2 + 0.006).multiply(scale);
      const back = new Matrix4().makeRotationY(Math.PI).multiply(new Matrix4().makeTranslation(0, 0, SLAB_DEPTH / 2 + 0.006)).multiply(scale);
      return [front, back] as const;
    });
    return { count: labelled.length, texture, geometry, material: labelMaterial(texture), faces };
  }, [nodes]);

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

  useEffect(() => () => buckets.forEach((b) => b.geometry.dispose()), [buckets]);
  useEffect(() => () => [bodyMaterial, glowMaterial].forEach((m) => m.dispose()), [bodyMaterial, glowMaterial]);
  useEffect(
    () => () => {
      labels.texture.dispose();
      labels.geometry.dispose();
      labels.material.dispose();
    },
    [labels],
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
    for (const mesh of [cometMesh.current, ringMesh.current, ...bodies.current, ...glows.current]) {
      if (mesh && !mesh.instanceColor) mesh.setColorAt(0, COLORS.spark);
    }
    cometMaterial.needsUpdate = ringMaterial.needsUpdate = bodyMaterial.needsUpdate = glowMaterial.needsUpdate = true;
  }, [cometMaterial, ringMaterial, bodyMaterial, glowMaterial, buckets]);

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
    const slab = scratch.object;
    const labelInstances = labelMesh.current;
    nodes.forEach((n, i) => {
      const [bucket, k] = place[i]!;
      const body = bodies.current[bucket];
      const halo = glows.current[bucket];
      if (!body || !halo) return;
      const flash = feed.nodeFlash.get(n.page);
      const g = glow(flash, now, flashMs);
      const away = scatter[i]!;
      const bob = reduced ? 0 : Math.sin(t * 0.8 + i * 0.7) * 0.05;
      const p = positions[i]!;
      p.set(n.x + away.x * shatter * 7, n.y + bob + away.y * shatter * 5, n.z + away.z * shatter * 7);
      slab.position.copy(p);
      slab.rotation.set(away.x * shatter * 3, n.angle, away.z * shatter * 3);
      slab.scale.setScalar((1 + g * 0.16) * (hovered.current === i ? 1.06 : 1));
      slab.updateMatrix();
      body.setMatrixAt(k, slab.matrix);
      if (labelInstances && i < labels.count) {
        const [front, back] = labels.faces[i]!;
        labelInstances.setMatrixAt(i, scratch.matrix.multiplyMatrices(slab.matrix, front));
        labelInstances.setMatrixAt(labels.count + i, scratch.matrix.multiplyMatrices(slab.matrix, back));
      }

      color.copy(n.leaf ? COLORS.leaf : COLORS.internal);
      if (hovered.current === i) color.multiplyScalar(1.35);
      if (shatter > 0) color.lerp(COLORS.danger, shatter * 0.8);
      body.setColorAt(k, color);
      // The glow shell, slightly larger, is black (invisible, blended additively) unless the node flashes.
      slab.scale.multiplyScalar(1.05);
      slab.updateMatrix();
      halo.setMatrixAt(k, slab.matrix);
      if (g > 0) color.copy(flash!.kind === "split" ? COLORS.split : COLORS.visit).multiplyScalar(g * 1.1);
      else color.setRGB(shatter * 0.5, 0, 0);
      halo.setColorAt(k, color);
    });
    for (const mesh of [...bodies.current, ...glows.current, labelInstances]) {
      if (!mesh) continue;
      mesh.instanceMatrix.needsUpdate = true;
      if (mesh.instanceColor) mesh.instanceColor.needsUpdate = true;
      mesh.boundingSphere = null; // recomputed when hovering, since the slabs move
    }
    labels.material.uniforms.opacity!.value = 1 - shatter * 0.7;

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

  const hover = (members: number[]) => (e: ThreeEvent<PointerEvent>) => {
    e.stopPropagation();
    const i = e.instanceId !== undefined ? members[e.instanceId] : undefined;
    hovered.current = i ?? null;
    onHover(i !== undefined ? nodes[i]!.page : null, e);
  };
  const leave = () => {
    hovered.current = null;
    onHover(null);
  };

  return (
    <group>
      {buckets.map((b, i) => (
        <group key={`${nodes.length}-${i}`}>
          <instancedMesh
            ref={(m) => void (bodies.current[i] = m)}
            args={[b.geometry, bodyMaterial, b.members.length]}
            frustumCulled={false}
            onPointerMove={hover(b.members)}
            onPointerOut={leave}
          />
          <instancedMesh
            ref={(m) => void (glows.current[i] = m)}
            args={[b.geometry, glowMaterial, b.members.length]}
            frustumCulled={false}
            raycast={noRaycast}
          />
        </group>
      ))}
      {labels.count > 0 && (
        <instancedMesh
          key={`labels-${labels.count}`}
          ref={labelMesh}
          args={[labels.geometry, labels.material, labels.count * 2]}
          frustumCulled={false}
          raycast={noRaycast}
        />
      )}
      <primitive object={pointer.line} raycast={noRaycast} frustumCulled={false} />
      <primitive object={chain.line} raycast={noRaycast} frustumCulled={false} />
      <instancedMesh ref={cometMesh} args={[cometGeometry, cometMaterial, COMETS * TRAIL]} frustumCulled={false} raycast={noRaycast} />
      <instancedMesh ref={ringMesh} args={[ringGeometry, ringMaterial, RINGS]} frustumCulled={false} raycast={noRaycast} />
    </group>
  );
});

const scratch = { object: new Object3D(), color: new Color(), matrix: new Matrix4() };

function noRaycast() {
  // Decorations are not hoverable; only the slab bodies are.
}
