import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  base: "./",
  build: {
    license: { fileName: "THIRD_PARTY_LICENSES.txt" },
    cssMinify: true,
    minify: "oxc",
    reportCompressedSize: false,
    sourcemap: false,
  },
});
