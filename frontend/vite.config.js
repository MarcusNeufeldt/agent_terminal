import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// Build output is served by the Python terminal server (server.py) from
// terminal/static/. In dev, /api is proxied to the running terminal.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  build: {
    outDir: '../terminal/static',
    emptyOutDir: true,
  },
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:8787',
    },
  },
})
