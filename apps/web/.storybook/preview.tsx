import type { Preview } from "@storybook/nextjs-vite";
import { withThemeByClassName } from "@storybook/addon-themes";

import { ScreenHarness, type ScreenState } from "../src/lib/stories/harness";

import "../src/app/globals.css";

/**
 * Both themes via a toolbar toggle (TITAN-FRT-011/012). The app uses the `.dark`
 * class strategy (light is the token default at `:root`), so the decorator adds
 * `dark` for the dark theme and nothing for light.
 *
 * The `screenState` parameter is what turns a story into one of TITAN-FRT-002's
 * four states: a story that sets it is wrapped in the real provider stack with
 * the network stubbed for that state (see `src/lib/stories/harness.tsx`). A
 * story that omits it renders exactly as before, so the eleven pre-existing
 * component stories are unaffected.
 */
const preview: Preview = {
  parameters: {
    controls: { matchers: { color: /(background|color)$/i, date: /Date$/i } },
    backgrounds: { disable: true },
    // Screens call `useRouter`/`useSearchParams`. Without the App Router mock a
    // screen story does not render a degraded screen — it throws on mount.
    nextjs: { appDirectory: true },
  },
  decorators: [
    withThemeByClassName({
      themes: { light: "", dark: "dark" },
      defaultTheme: "dark",
    }),
    (Story, context) => {
      const state = context.parameters.screenState as ScreenState | undefined;
      if (!state) {
        return (
          <div className="bg-background p-6 text-foreground">
            <Story />
          </div>
        );
      }
      return (
        <ScreenHarness
          state={state}
          overrides={context.parameters.apiOverrides}
        >
          {/*
           * No padding wrapper for a screen. Screens own their own layout, and
           * §8's 375 px behaviour is a property of that layout — 24 px of story
           * chrome would make every narrow snapshot lie about it.
           */}
          <div className="bg-background text-foreground">
            <Story />
          </div>
        </ScreenHarness>
      );
    },
  ],
};

export default preview;
