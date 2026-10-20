import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 后端地址从环境变量读，代码里不硬编码（项目硬约束：地址一律走配置）。
const API_TARGET = process.env.VITE_API_TARGET || 'http://127.0.0.1:8000'

// 端口默认 5174：当年旧大屏（frontend/dashboard）占着 5173，两套要能同时开着做对照。
// 旧大屏已于 2026-10-19 移除，5173 空出来了 —— 但**故意不改**，
// 免得已经开着浏览器的人突然打不开。想换就设环境变量 VITE_PORT。
const PORT = Number(process.env.VITE_PORT || 5174)

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
    // 端口被占就报错退出，不要偷偷换一个 —— 否则你打开的是旧大屏还以为新前端没生效
    strictPort: true,
    proxy: {
      // 同源转发 /api：浏览器看到的是同源请求，后端不需要开 CORS。
      '/api': {
        target: API_TARGET,
        changeOrigin: true,
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
    chunkSizeWarningLimit: 1600,
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
