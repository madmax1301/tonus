import adapter from '@sveltejs/adapter-static';
import { sveltekit } from '@sveltejs/kit/vite';
import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';
import tailwindcss from '@tailwindcss/vite';
import { defineConfig } from 'vite';

// SvelteKit 3: die frühere svelte.config.js lebt jetzt hier als Optionen
// des sveltekit()-Plugins.
const basePath = (process.env.BASE_PATH || '') as '' | `/${string}`;

export default defineConfig({
  plugins: [
    tailwindcss(),
    sveltekit({
      preprocess: vitePreprocess(),
      adapter: adapter({
        pages: 'build',
        assets: 'build',
        fallback: 'index.html',
        precompress: false,
        strict: true
      }),
      paths: {
        base: basePath,
        relative: false
      }
    })
  ],
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
