import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const DOC_API_DOMAIN = 'https://open.hoyowave.com'

export default defineConfig({
  plugins: [react()],
  server: {
    // 监听 0.0.0.0，局域网内同事可通过本机 IP 访问
    host: true,
    proxy: {
      '/llm-api': {
        target: 'https://llm-open-ai-private.mihoyo.com',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/llm-api/, ''),
      },
      '/doc-api': {
        target: DOC_API_DOMAIN,
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/doc-api/, ''),
      },
      // MCP（本地 openapi-mcp），走同源代理避免 CORS
      '/mcp-api': {
        target: 'http://127.0.0.1:5524',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/mcp-api/, ''),
      },
    },
  },
})
