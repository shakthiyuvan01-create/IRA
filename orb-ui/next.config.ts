import type { NextConfig } from "next";
import { fileURLToPath } from "node:url";
import path from "node:path";

// Keep this app's build self-contained even when nested under a larger repo
// (e.g. IRA) that has its own lockfiles elsewhere on the filesystem.
const nextConfig: NextConfig = {
  turbopack: {
    root: path.dirname(fileURLToPath(import.meta.url)),
  },
};

export default nextConfig;
