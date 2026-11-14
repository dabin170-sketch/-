import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 빌드 결과는 backend/static 에 놓여 FastAPI가 함께 제공한다 (Render 서비스 1개로 운영).
export default defineConfig({
  plugins: [react()],
  build: { outDir: '../backend/static', emptyOutDir: true },
  server: { proxy: { '/api': 'http://localhost:8000' } },
})
