// The write-ahead log as a river of light spiralling down beside the tree. Every logged page drops in
// as a mote of light; a sync makes them shine; the commit frame burns gold; a checkpoint pours the
// whole river into the floor (the data file) and it starts again.
import { useFrame } from "@react-three/fiber";
import { memo, useEffect, useLayoutEffect, useMemo, useRef } from "react";
import { BoxGeometry, Color, InstancedMesh, MeshBasicMaterial, Object3D, Vector3 } from "three";
import type { FeedState, WalFrame } from "../feed";
import { COLORS, FLOOR_Y, RIVER_RADIUS, TREE_TOP } from "./world";

const CAPACITY = 512;
const SLOTS = 240; // frames the river shows before the oldest scroll away
const DRAIN_MS = 1100;

interface Mote {
  position: Vector3;
  frame: WalFrame;
}

interface Drop {
  from: Vector3;
  frame: WalFrame;
  start: number;
}

export const LogRiver = memo(function LogRiver({ feed, x }: { feed: FeedState; x: number }) {
  const mesh = useRef<InstancedMesh>(null);
  const geometry = useMemo(() => new BoxGeometry(0.14, 0.14, 0.14), []);
  const material = useMemo(() => new MeshBasicMaterial({ toneMapped: false }), []);
  const motes = useRef(new Map<number, Mote>());
  const draining = useRef<Drop[]>([]);
  const lastCheckpoint = useRef<number | null>(feed.checkpointAt);

  useEffect(() => () => {
    geometry.dispose();
    material.dispose();
  }, [geometry, material]);

  useLayoutEffect(() => {
    if (mesh.current && !mesh.current.instanceColor) mesh.current.setColorAt(0, COLORS.pending);
    material.needsUpdate = true;
  }, [material]);

  useFrame((_, delta) => {
    const instances = mesh.current;
    if (!instances) return;
    const now = performance.now();
    const dummy = scratch.object;
    const color = scratch.color;
    const step = (TREE_TOP + 1 - FLOOR_Y) / SLOTS;
    const ease = 1 - Math.exp(-delta * 7);

    // A checkpoint empties the log: whatever the river held pours into the floor.
    if (feed.checkpointAt !== lastCheckpoint.current) {
      lastCheckpoint.current = feed.checkpointAt;
      for (const mote of motes.current.values()) draining.current.push({ from: mote.position, frame: mote.frame, start: now });
      motes.current.clear();
    }

    const frames = feed.wal.slice(-SLOTS);
    const live = new Set(frames.map((f) => f.id));
    for (const id of motes.current.keys()) if (!live.has(id)) motes.current.delete(id); // rolled back
    let n = 0;
    frames.forEach((frame, slot) => {
      const angle = slot * 0.42;
      const target = scratch.target.set(x + Math.cos(angle) * RIVER_RADIUS, FLOOR_Y + 0.5 + slot * step, Math.sin(angle) * RIVER_RADIUS);
      let mote = motes.current.get(frame.id);
      if (!mote) {
        mote = { position: target.clone().setY(TREE_TOP + 3), frame };
        motes.current.set(frame.id, mote);
      }
      mote.frame = frame;
      mote.position.lerp(target, ease);
      dummy.position.copy(mote.position);
      dummy.scale.setScalar(frame.commit ? 1.9 : 1);
      dummy.rotation.set(0, angle, 0);
      dummy.updateMatrix();
      instances.setMatrixAt(n, dummy.matrix);
      color.copy(frame.commit ? COLORS.commit : frame.committed ? COLORS.synced : COLORS.pending);
      instances.setColorAt(n, color.multiplyScalar(frame.commit ? 3 : frame.committed ? 2 : 0.9));
      n++;
    });

    draining.current = draining.current.filter((d) => now - d.start < DRAIN_MS);
    for (const d of draining.current) {
      if (n >= CAPACITY) break;
      const k = (now - d.start) / DRAIN_MS;
      dummy.position.lerpVectors(d.from, scratch.floor.set(0, FLOOR_Y, 1), k * k);
      dummy.scale.setScalar(1 - k);
      dummy.updateMatrix();
      instances.setMatrixAt(n, dummy.matrix);
      instances.setColorAt(n, color.copy(COLORS.synced).multiplyScalar(2 * (1 - k)));
      n++;
    }
    instances.count = n;
    instances.instanceMatrix.needsUpdate = true;
    if (instances.instanceColor) instances.instanceColor.needsUpdate = true;
  });

  return <instancedMesh ref={mesh} args={[geometry, material, CAPACITY]} frustumCulled={false} raycast={() => undefined} />;
});

const scratch = { object: new Object3D(), color: new Color(), target: new Vector3(), floor: new Vector3() };
