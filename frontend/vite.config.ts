import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:8000',
      // Trailing slash matters: '/resume' (no slash) as a prefix would also
      // match '/resumes', the SPA's own route, and silently proxy it to the
      // backend's old Jinja page instead of letting Vite serve the SPA.
      '/resume/': 'http://127.0.0.1:8000',
    },
  },
})
