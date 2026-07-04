/// <reference types="vitest/config" />
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { fileURLToPath, URL } from 'node:url'

// 悬壶 WebUI —— Vite 配置
// dev 阶段通过 server.proxy 将同源 /api 请求转发到本地 FastAPI 后端，
// 避免在浏览器里直接写死跨域地址，也避免开发期 CORS 配置。
// 生产由 Nginx 同源反代，前端只发相对路径请求。
const BACKEND_TARGET = process.env.VITE_DEV_PROXY_TARGET ?? 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    host: '127.0.0.1',
    port: 5173,
    proxy: {
      // 会话、消息、SSE、恢复、review、健康检查全部走 /api 前缀。
      // SSE（/stream）必须显式关闭 ws 之外的特殊处理，并保留 keep-alive。
      '/api': {
        target: BACKEND_TARGET,
        changeOrigin: true,
        // SSE 长连接需要禁用代理缓冲，否则事件会被攒在缓冲区不推送。
        // Vite 的 http-proxy 通过 headers 控制；这里不压缩、不缓冲。
        configure: (proxy) => {
          proxy.on('proxyReq', (proxyReq) => {
            proxyReq.setHeader('X-Forwarded-Host', proxyReq.getHeader('host') ?? '')
          })
        },
      },
    },
  },
  test: {
    environment: 'jsdom',
    globals: false,
    setupFiles: ['./src/test/setup.ts'],
    include: ['src/**/*.{test,spec}.{ts,tsx}'],
    css: false,
  },
})
