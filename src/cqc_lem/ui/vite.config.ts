import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    // Source maps exist only so PostHog error tracking (issue #648) can un-minify a stack trace.
    // The image build uploads them and then DELETES every .map from dist, so they are never served.
    sourcemap: true,
  },
})
