/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,jsx}"],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        // Institutional deep-navy / charcoal ramp. The whole app is built on
        // Tailwind's `gray-*` scale, so remapping it here reskins every panel,
        // border and background at once — Bloomberg-terminal style, never pure
        // black. Light shades stay near-white for primary text.
        gray: {
          50: "#f4f6fb",
          100: "#e6eaf2",
          200: "#cdd5e3",
          300: "#a7b2c7",
          400: "#7e8aa3",
          500: "#5c6880",
          600: "#3b4660",
          700: "#262f44", // hairline borders / dividers
          750: "#1f2638",
          800: "#161d2e", // panel / surface
          850: "#111726",
          900: "#0b1019", // app background (deep navy)
          950: "#070a12",
        },
        // Semantic accents (used by new institutional components).
        accent: {
          DEFAULT: "#3b82f6",
          soft: "#60a5fa",
        },
        profit: "#22c55e",
        loss: "#ef4444",
        warn: "#f59e0b",
        info: "#38bdf8",
      },
      fontFamily: {
        mono: [
          "ui-monospace",
          "SFMono-Regular",
          "Menlo",
          "Consolas",
          "Roboto Mono",
          "monospace",
        ],
      },
      borderRadius: {
        // Institutional dashboards favour sharp edges; cap radius low.
        lg: "4px",
        md: "3px",
        sm: "2px",
      },
      keyframes: {
        flashUp: {
          "0%": { backgroundColor: "rgba(34,197,94,0.28)" },
          "100%": { backgroundColor: "transparent" },
        },
        flashDown: {
          "0%": { backgroundColor: "rgba(239,68,68,0.28)" },
          "100%": { backgroundColor: "transparent" },
        },
      },
      animation: {
        flashUp: "flashUp 0.9s ease-out",
        flashDown: "flashDown 0.9s ease-out",
      },
    },
  },
  plugins: [],
};
