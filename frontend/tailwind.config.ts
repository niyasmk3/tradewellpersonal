import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./app/**/*.{ts,tsx}",
    "./components/**/*.{ts,tsx}",
    "./lib/**/*.{ts,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        // Trading-desk palette
        panel: "#0f141b",
        panel2: "#151c25",
        edge: "#1f2937",
        bull: "#16c784",
        bear: "#ea3943",
        accent: "#3b82f6",
        muted: "#8b98a9",
      },
      fontFamily: {
        mono: ["ui-monospace", "SFMono-Regular", "Menlo", "monospace"],
      },
    },
  },
  plugins: [],
};

export default config;
