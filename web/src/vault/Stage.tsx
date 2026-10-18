// Camera, controls and glow for the vault.
import { useFrame, useThree } from "@react-three/fiber";
import { useCallback, useEffect, useMemo, useRef } from "react";
import { Vector2 } from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { EffectComposer } from "three/addons/postprocessing/EffectComposer.js";
import { OutputPass } from "three/addons/postprocessing/OutputPass.js";
import { RenderPass } from "three/addons/postprocessing/RenderPass.js";
import { UnrealBloomPass } from "three/addons/postprocessing/UnrealBloomPass.js";

export interface Bounds {
  centerX: number;
  centerY: number;
  width: number;
  height: number;
}

/**
 * Orbit controls, and a camera that frames the scene. It reframes when `resetKey` changes, and also
 * when `fitKey` changes (the scene grew or the stage changed shape) as long as the viewer has not
 * moved the camera themselves.
 */
export function Rig({ bounds, fitKey, resetKey, elevation }: {
  bounds: Bounds;
  fitKey: string;
  resetKey: string;
  elevation: number; // camera height as a share of its distance: higher looks down more
}) {
  const { camera, gl, size } = useThree();
  // Refit when the stage changes shape noticeably, not on every pixel of a resize.
  const aspect = Math.round((size.width / Math.max(size.height, 1)) * 10) / 10;
  const controls = useMemo(() => new OrbitControls(camera, gl.domElement), [camera, gl]);
  const boundsRef = useRef(bounds);
  boundsRef.current = bounds;
  const elevationRef = useRef(elevation);
  elevationRef.current = elevation;
  const viewerMoved = useRef(false);

  useEffect(() => {
    controls.enableDamping = true;
    controls.dampingFactor = 0.08;
    controls.maxPolarAngle = Math.PI * 0.62; // never look up from under the floor
    controls.minDistance = 4;
    controls.maxDistance = 140;
    const moved = () => {
      viewerMoved.current = true;
    };
    controls.addEventListener("start", moved);
    return () => {
      controls.removeEventListener("start", moved);
      controls.dispose();
    };
  }, [controls]);

  const frame = useCallback(() => {
    const b = boundsRef.current;
    const fov = ("fov" in camera ? (camera.fov as number) : 45) * (Math.PI / 180);
    const tan = Math.tan(fov / 2);
    // Far enough that both the scene's height and its width fit the stage.
    const fit = Math.max(b.height / (2 * tan), b.width / (2 * tan * Math.max(aspect, 0.3)));
    const distance = Math.min(Math.max(fit, 8), 120);
    controls.target.set(b.centerX, b.centerY, 0);
    camera.position.set(b.centerX, b.centerY + distance * elevationRef.current, distance);
    controls.update();
  }, [aspect, camera, controls]);

  useEffect(() => {
    viewerMoved.current = false;
    frame();
  }, [resetKey, frame]);

  useEffect(() => {
    if (!viewerMoved.current) frame();
  }, [fitKey, frame]);

  useFrame(() => controls.update());
  return null;
}

/** Renders the scene through a bloom pass; the high threshold keeps the glow to flashes and lit edges. Takes over R3F's render loop. */
export function Bloom() {
  const { gl, scene, camera, size } = useThree();
  const composer = useMemo(() => {
    const c = new EffectComposer(gl);
    c.addPass(new RenderPass(scene, camera));
    c.addPass(new UnrealBloomPass(new Vector2(size.width, size.height), 0.62, 0.55, 0.86));
    c.addPass(new OutputPass());
    return c;
  }, [gl, scene, camera]); // size is applied by the effect below, so resizing never rebuilds the passes

  useEffect(() => {
    composer.setPixelRatio(gl.getPixelRatio());
    composer.setSize(size.width, size.height);
  }, [composer, gl, size.width, size.height]);

  useEffect(() => () => composer.dispose(), [composer]);

  useFrame((_, delta) => composer.render(delta), 1);
  return null;
}
