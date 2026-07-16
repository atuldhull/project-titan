import { fileURLToPath } from "node:url";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

const resolvePath = (path: string) =>
  fileURLToPath(new URL(path, import.meta.url));

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@titan/schemas": resolvePath("../../packages/schemas/src/index.ts"),
      "@titan/api-client": resolvePath("../../packages/api-client/src/index.ts"),
      "@titan/ws-protocol": resolvePath(
        "../../packages/ws-protocol/src/index.ts",
      ),
      "@": resolvePath("./src"),
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./tests/setup.ts"],
    include: ["tests/**/*.test.{ts,tsx}", "src/**/*.test.{ts,tsx}"],
  },
});
