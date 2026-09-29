"use client";

import type { ReactNode } from "react";
import { motion } from "framer-motion";

interface FadeInProps {
  children: ReactNode;
  delay?: number;
  y?: number;
  /** Pass a className through for layout sx-in-Box composition where needed. */
  style?: React.CSSProperties;
}

/**
 * Shared entrance animation primitive — fades and lifts content in once,
 * on mount. Used for page headers, section panels, and staggered list
 * items across the app so every "content appears" moment shares the same
 * feel instead of each screen inventing its own transition.
 */
export default function FadeIn({ children, delay = 0, y = 12, style }: FadeInProps) {
  return (
    <motion.div
      initial={{ opacity: 0, y }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.45, delay, ease: [0.16, 1, 0.3, 1] as const }}
      style={style}
    >
      {children}
    </motion.div>
  );
}

/** Stagger container — wrap a list of FadeIn/StaggerItem children to have
 * them animate in sequence rather than all at once. */
export const staggerContainer = {
  hidden: {},
  show: {
    transition: { staggerChildren: 0.06 },
  },
} as const;

export const staggerItem = {
  hidden: { opacity: 0, y: 14 },
  show: { opacity: 1, y: 0, transition: { duration: 0.4, ease: [0.16, 1, 0.3, 1] as const } },
} as const;
