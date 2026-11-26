import { defineConfig } from "vitest/config";
import { svelte } from "@sveltejs/vite-plugin-svelte";

// Tauri 开发模式固定用 1420 端口
export default defineConfig({
  plugins: [svelte()],
  clearScreen: false,
  server: { port: 1420, strictPort: true },
  build: { target: "es2021", chunkSizeWarningLimit: 1200 },
  test: { environment: "node", include: ["src/**/*.test.ts"] },
});
