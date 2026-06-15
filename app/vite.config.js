import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// base: "./" — обов'язково для Capacitor, щоб шляхи до файлів були відносні
export default defineConfig({
  plugins: [react()],
  base: "./",
  build: { outDir: "dist" },
});
