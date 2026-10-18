// The vault's surroundings: a gradient sky with slow-turning stars, a soft studio environment for the
// glass to reflect, a key light with two coloured rim lights, and a polished floor under the tree.
import { useFrame, useThree } from "@react-three/fiber";
import { memo, useEffect, useMemo, useRef, type RefObject } from "react";
import {
  AdditiveBlending,
  BackSide,
  BufferAttribute,
  BufferGeometry,
  CanvasTexture,
  CircleGeometry,
  Color,
  MeshStandardMaterial,
  PMREMGenerator,
  Points,
  PointsMaterial,
  ShaderMaterial,
  SphereGeometry,
  SRGBColorSpace,
} from "three";
import { RoomEnvironment } from "three/addons/environments/RoomEnvironment.js";
import { COLORS, FLOOR_Y, type Motion } from "./world";

const STARS = 1400;
const FLOOR_RADIUS = 140; // wide enough that its edge is lost in the fog

const skyShader = {
  uniforms: {
    top: { value: new Color("#1a1446") },
    middle: { value: new Color("#070a1c") },
    bottom: { value: new Color("#02030a") },
  },
  vertexShader: `
    varying vec3 vDir;
    void main() {
      vDir = normalize(position);
      gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
    }`,
  fragmentShader: `
    uniform vec3 top;
    uniform vec3 middle;
    uniform vec3 bottom;
    varying vec3 vDir;
    void main() {
      float h = vDir.y;
      vec3 c = h > 0.0 ? mix(middle, top, pow(h, 0.7)) : mix(middle, bottom, pow(-h, 0.5));
      gl_FragColor = vec4(c, 1.0);
    }`,
};

/** The floor's glow: a soft pool of light under the tree and a few faint rings, fading to the edge. */
function floorGlow(): CanvasTexture {
  const size = 2048;
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = size;
  const ctx = canvas.getContext("2d")!;
  const c = size / 2;
  // Painted over opaque black: an emissive map ignores alpha, so translucent pixels would glow at full strength.
  ctx.fillStyle = "#000";
  ctx.fillRect(0, 0, size, size);
  const pool = ctx.createRadialGradient(c, c, 0, c, c, c * 0.13);
  pool.addColorStop(0, "rgba(120,90,255,0.4)");
  pool.addColorStop(0.45, "rgba(70,80,220,0.12)");
  pool.addColorStop(1, "rgba(40,60,200,0)");
  ctx.fillStyle = pool;
  ctx.fillRect(0, 0, size, size);
  for (const [r, alpha] of [[0.04, 0.3], [0.065, 0.16], [0.09, 0.07]] as const) {
    ctx.strokeStyle = `rgba(140,160,255,${alpha})`;
    ctx.lineWidth = 3;
    ctx.beginPath();
    ctx.arc(c, c, c * r, 0, Math.PI * 2);
    ctx.stroke();
  }
  const texture = new CanvasTexture(canvas);
  texture.colorSpace = SRGBColorSpace;
  texture.anisotropy = 8;
  return texture;
}

export const Scenery = memo(function Scenery({ motion }: { motion: RefObject<Motion> }) {
  const { gl, scene } = useThree();
  const stars = useRef<Points>(null);

  // A neutral studio, prefiltered for reflections, so the glass slabs and floor catch soft highlights.
  useEffect(() => {
    const pmrem = new PMREMGenerator(gl);
    const environment = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;
    scene.environment = environment;
    scene.environmentIntensity = 0.45;
    return () => {
      scene.environment = null;
      environment.dispose();
      pmrem.dispose();
    };
  }, [gl, scene]);

  const sky = useMemo(() => ({ geometry: new SphereGeometry(320, 32, 16), material: new ShaderMaterial({ ...skyShader, side: BackSide, depthWrite: false }) }), []);
  const starField = useMemo(() => {
    const positions = new Float32Array(STARS * 3);
    for (let i = 0; i < STARS; i++) {
      // Spread over the upper part of a sphere, so stars never sit under the floor.
      const u = Math.random() * Math.PI * 2;
      const v = Math.acos(1 - Math.random() * 1.1);
      const r = 140 + Math.random() * 60;
      positions[i * 3] = r * Math.sin(v) * Math.cos(u);
      positions[i * 3 + 1] = r * Math.cos(v) - 20;
      positions[i * 3 + 2] = r * Math.sin(v) * Math.sin(u);
    }
    const geometry = new BufferGeometry();
    geometry.setAttribute("position", new BufferAttribute(positions, 3));
    const material = new PointsMaterial({ color: "#c9d4ff", size: 0.7, sizeAttenuation: true, transparent: true, opacity: 0.75, blending: AdditiveBlending, depthWrite: false });
    return { geometry, material };
  }, []);
  const floor = useMemo(() => {
    const grid = floorGlow();
    return {
      geometry: new CircleGeometry(FLOOR_RADIUS, 96),
      material: new MeshStandardMaterial({ color: COLORS.floor, metalness: 0.2, roughness: 0.6, envMapIntensity: 0.08, emissive: new Color("#ffffff"), emissiveMap: grid, emissiveIntensity: 0.55 }),
      grid,
    };
  }, []);

  useEffect(
    () => () => {
      sky.geometry.dispose();
      sky.material.dispose();
      starField.geometry.dispose();
      starField.material.dispose();
      floor.geometry.dispose();
      floor.material.dispose();
      floor.grid.dispose();
    },
    [sky, starField, floor],
  );

  useFrame((_, delta) => {
    if (stars.current && !motion.current?.reduced) stars.current.rotation.y += delta * 0.006;
  });

  return (
    <group>
      <mesh geometry={sky.geometry} material={sky.material} renderOrder={-1} raycast={() => undefined} />
      <points ref={stars} geometry={starField.geometry} material={starField.material} raycast={() => undefined} />
      <ambientLight intensity={0.25} />
      <hemisphereLight args={["#6f7fd6", "#05070f", 0.25]} />
      <directionalLight position={[6, 16, 12]} intensity={2.2} color="#e8eeff" />
      <pointLight position={[-14, 10, 8]} intensity={120} distance={70} decay={2} color="#c45cff" />
      <pointLight position={[14, 12, -8]} intensity={90} distance={70} decay={2} color="#3fc7ff" />
      <mesh geometry={floor.geometry} material={floor.material} rotation={[-Math.PI / 2, 0, 0]} position={[0, FLOOR_Y - 0.12, 0]} raycast={() => undefined} />
    </group>
  );
});
