import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import path from 'path'
import fs from 'fs'

// 智能判断打包环境：如果后端目录不存在（如 Docker 多阶段构建中），则输出到默认的 dist 目录以契合 Dockerfile 配置；
// 如果在本地开发，则直接输出到后端的 static 目录以方便本地测试。
const hasBackend = fs.existsSync(path.resolve(__dirname, '../backend'))
const outDir = hasBackend ? '../backend/static' : 'dist'

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  build: {
    outDir: outDir,
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
      '/ws': {
        target: 'http://localhost:8000',
        ws: true,
      },
      '/static': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
})
