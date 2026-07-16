import path from "path";
import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Vol 9 §3.3: the titan-frontend image ships Next.js standalone output.
  output: "standalone",
  // The workspace packages (@titan/schemas|api-client|ws-protocol) live at the
  // monorepo root and are consumed as source via tsconfig `paths`; trace from
  // the repo root so the standalone bundle includes them.
  outputFileTracingRoot: path.join(__dirname, "../../"),
};

export default nextConfig;
