import type { StorybookConfig } from "@storybook/nextjs-vite";

/**
 * Storybook (TITAN-FRT-002) — the home for the per-state stories (loading /
 * empty / error / populated) later phases require, rendered in both themes via
 * the theme toolbar wired in preview.tsx.
 *
 * Uses the Vite-based Next framework: the webpack-based `@storybook/nextjs`
 * clashes with Next 15.5's bundled webpack (two webpack instances → DefinePlugin
 * "compilation argument" error).
 */
const config: StorybookConfig = {
  stories: ["../src/**/*.stories.@(ts|tsx)"],
  addons: ["@storybook/addon-themes"],
  framework: { name: "@storybook/nextjs-vite", options: {} },
  staticDirs: ["../public"],
};

export default config;
