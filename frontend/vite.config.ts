import { sveltekit } from '@sveltejs/kit/vite';
import tailwindcss from '@tailwindcss/vite';
import { defineConfig } from 'vite';

export default defineConfig({
  plugins: [tailwindcss(), sveltekit()],
  server: {
    port: 5173,
    proxy: {
      // Während des Dev-Servers werden /api/* an das FastAPI-Backend
      // weitergereicht, damit man SvelteKit ohne Container-Build entwickeln kann.
      '/api': {
        target: 'http://localhost:8088',
        changeOrigin: true
      }
    }
  }
});
