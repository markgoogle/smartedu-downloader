import { defineConfig } from 'vite'
import preact from '@preact/preset-vite'

// 极小产物：Preact (~4KB gzip) + 单文件构建，输出到 web/dist 由 FastAPI 直接托管
export default defineConfig({
  plugins: [preact()],
  build: {
    outDir: 'dist',
    target: 'es2020',
    cssCodeSplit: false,
    rollupOptions: {
      output: {
        entryFileNames: 'assets/app-[hash].js',
        assetFileNames: 'assets/app-[hash][extname]',
      },
    },
  },
  server: {
    proxy: { '/api': 'http://127.0.0.1:8000' },
  },
})
