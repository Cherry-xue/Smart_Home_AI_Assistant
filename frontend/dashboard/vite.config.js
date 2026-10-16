import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 后端地址从环境变量读，代码里不硬编码（项目硬约束：地址一律走配置）。
// 默认值是本机 8000 —— 与 backend/run_api.py 的默认端口保持一致。
const API_TARGET = process.env.VITE_API_TARGET || 'http://127.0.0.1:8000'
const PORT = Number(process.env.VITE_PORT || 5173)

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: PORT,
    host: '127.0.0.1',
    strictPort: false,
    proxy: {
      // 同源转发 /api：浏览器看到的是同源请求，后端不需要开 CORS。
      // ⚠️ SSE 必须关掉缓冲，否则事件会被攒着一起发，流式就没意义了。
      '/api': {
        target: API_TARGET,
        changeOrigin: true,
        // ws: false —— SSE 是普通 HTTP，不要按 websocket 升级
        configure: (proxy) => {
          proxy.on('proxyReq', (proxyReq) => {
            proxyReq.setHeader('X-Forwarded-Proto', 'http')
          })
        },
      },
    },
  },
  build: {
    outDir: 'dist',
    chunkSizeWarningLimit: 1600, // Element Plus + ECharts 全量引入，体积本来就大
    rollupOptions: {
      output: {
        manualChunks: {
          echarts: ['echarts'],
          'element-plus': ['element-plus'],
          vue: ['vue', 'vue-router', 'pinia'],
        },
      },
    },
  },
})
