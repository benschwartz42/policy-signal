import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Relative base so the build works whether served from a GitHub Pages
// project subpath (/repo/) or a custom domain root.
export default defineConfig({
  base: "./",
  plugins: [react()],
});
