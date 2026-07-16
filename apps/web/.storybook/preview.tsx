import type { Preview } from "@storybook/nextjs-vite";
import { withThemeByClassName } from "@storybook/addon-themes";

import "../src/app/globals.css";

/**
 * Both themes via a toolbar toggle (TITAN-FRT-011/012). The app uses the `.dark`
 * class strategy (light is the token default at `:root`), so the decorator adds
 * `dark` for the dark theme and nothing for light.
 */
const preview: Preview = {
  parameters: {
    controls: { matchers: { color: /(background|color)$/i, date: /Date$/i } },
    backgrounds: { disable: true },
  },
  decorators: [
    withThemeByClassName({
      themes: { light: "", dark: "dark" },
      defaultTheme: "dark",
    }),
    (Story) => (
      <div className="bg-background p-6 text-foreground">
        <Story />
      </div>
    ),
  ],
};

export default preview;
