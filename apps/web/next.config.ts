import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Vol 9 §3.3: the titan-frontend image ships Next.js standalone output.
  output: "standalone",
};

export default nextConfig;
