import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
// Finmeta serves the built UI from backend/finmeta/web (see api/app.py).
// The relative path from /work/ui is ../backend/finmeta/web.
var webOut = '../backend/finmeta/web';
export default defineConfig({
    plugins: [react()],
    base: '/',
    build: {
        outDir: webOut,
        emptyOutDir: true,
    },
});
