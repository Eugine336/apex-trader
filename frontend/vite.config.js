import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The backend CORS config allows http://localhost:3000 by default, so the dev
// server binds to port 3000 to match.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 3000,
    host: true,
  },
  preview: {
    port: 3000,
  },
});
