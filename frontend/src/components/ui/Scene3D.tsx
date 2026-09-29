"use client";

import { useRef, useState } from "react";
import { Canvas, useFrame, useThree } from "@react-three/fiber";
import * as THREE from "three";

interface Scene3DProps {
  accentColor: string;
  secondaryColor: string;
  nodeCount?: number;
}

// A tiny seeded PRNG (mulberry32) — deterministic, not React's impure
// Math.random(), so generating the node field is a pure computation that
// can safely run inside a lazy useState initializer (React's "components
// must be pure" rule flags Math.random() called during render, even
// inside useMemo; a seeded generator has no such issue since the same
// seed always produces the same output).
function mulberry32(seed: number): () => number {
  let a = seed;
  return () => {
    a |= 0;
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function buildNetwork(nodeCount: number, seed: number): { positions: Float32Array; linePositions: Float32Array } {
  const rand = mulberry32(seed);
  const positions = new Float32Array(nodeCount * 3);
  for (let i = 0; i < nodeCount; i++) {
    const radius = 5 + rand() * 4;
    const theta = rand() * Math.PI * 2;
    const phi = Math.acos(rand() * 2 - 1);
    positions[i * 3] = radius * Math.sin(phi) * Math.cos(theta);
    positions[i * 3 + 1] = radius * Math.sin(phi) * Math.sin(theta) * 0.5;
    positions[i * 3 + 2] = radius * Math.cos(phi) * 0.6 - 2;
  }

  // Connect each node to its nearest few neighbors so the field reads as a
  // network/graph (edges), not just scattered dots.
  const points: number[] = [];
  const maxDistSq = 3.2 * 3.2;
  const maxEdgesPerNode = 2;
  for (let i = 0; i < nodeCount; i++) {
    let edges = 0;
    for (let j = i + 1; j < nodeCount && edges < maxEdgesPerNode; j++) {
      const dx = positions[i * 3] - positions[j * 3];
      const dy = positions[i * 3 + 1] - positions[j * 3 + 1];
      const dz = positions[i * 3 + 2] - positions[j * 3 + 2];
      const distSq = dx * dx + dy * dy + dz * dz;
      if (distSq < maxDistSq) {
        points.push(positions[i * 3], positions[i * 3 + 1], positions[i * 3 + 2]);
        points.push(positions[j * 3], positions[j * 3 + 1], positions[j * 3 + 2]);
        edges++;
      }
    }
  }

  return { positions, linePositions: new Float32Array(points) };
}

/**
 * A real WebGL 3D scene: a field of connected points (a "lead network"
 * graph — nodes as companies/people, lines as discovered relationships,
 * matching the product's own domain) that slowly rotates and gently
 * responds to pointer movement (subtle parallax, not a hard follow).
 * Rendered as a fixed full-viewport <Canvas>, pointer-events disabled so
 * it never intercepts real UI interaction.
 */
function NetworkPoints({ accentColor, secondaryColor, nodeCount = 90 }: Required<Scene3DProps>) {
  const groupRef = useRef<THREE.Group>(null);
  const { viewport, pointer } = useThree();

  // Lazy useState initializer: buildNetwork runs exactly once, on first
  // render, and its result is cached for the component's lifetime — the
  // React-idiomatic "compute once" pattern.
  const [{ positions, linePositions }] = useState(() => buildNetwork(nodeCount, 1337));

  useFrame((state) => {
    if (!groupRef.current) return;
    const t = state.clock.getElapsedTime();
    // Slow autonomous rotation, plus a gentle parallax offset toward the
    // pointer position — never a hard "look at cursor" snap, just a soft
    // lean, clamped to a small range so it never feels jarring.
    groupRef.current.rotation.y = t * 0.03 + pointer.x * 0.15;
    groupRef.current.rotation.x = Math.sin(t * 0.15) * 0.05 + pointer.y * 0.08;
    groupRef.current.position.x = THREE.MathUtils.lerp(groupRef.current.position.x, pointer.x * 0.4, 0.02);
    groupRef.current.position.y = THREE.MathUtils.lerp(groupRef.current.position.y, pointer.y * 0.25, 0.02);
  });

  return (
    <group ref={groupRef} scale={Math.min(viewport.width, 14) / 10}>
      <points>
        <bufferGeometry>
          <bufferAttribute attach="attributes-position" args={[positions, 3]} />
        </bufferGeometry>
        <pointsMaterial color={accentColor} size={0.06} sizeAttenuation transparent opacity={0.85} />
      </points>
      <lineSegments>
        <bufferGeometry>
          <bufferAttribute attach="attributes-position" args={[linePositions, 3]} />
        </bufferGeometry>
        <lineBasicMaterial color={secondaryColor} transparent opacity={0.14} />
      </lineSegments>
    </group>
  );
}

export default function Scene3D({ accentColor, secondaryColor, nodeCount = 90 }: Scene3DProps) {
  return (
    <Canvas
      dpr={[1, 1.5]}
      camera={{ position: [0, 0, 9], fov: 50 }}
      gl={{ antialias: true, alpha: true }}
      style={{ position: "absolute", inset: 0, pointerEvents: "none" }}
    >
      <ambientLight intensity={0.6} />
      <NetworkPoints accentColor={accentColor} secondaryColor={secondaryColor} nodeCount={nodeCount} />
    </Canvas>
  );
}
