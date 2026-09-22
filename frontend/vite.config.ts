import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vitest/config'
import vue from '@vitejs/plugin-vue'
import AutoImport from 'unplugin-auto-import/vite'
import Components from 'unplugin-vue-components/vite'
import { ElementPlusResolver } from 'unplugin-vue-components/resolvers'

// https://vite.dev/config/
export default defineConfig({
  plugins: [
    vue(),
    // Explicit API imports stay the default style; auto-import is used only so the
    // Element Plus *global* APIs (ElMessage / ElMessageBox / ElNotification) pull their
    // own styles in. Components are auto-resolved below.
    AutoImport({
      imports: ['vue', 'vue-router', 'pinia'],
      dts: 'src/types/auto-imports.d.ts',
      resolvers: [ElementPlusResolver({ importStyle: 'sass' })],
      vueTemplate: true,
    }),
    Components({
      dts: 'src/types/components.d.ts',
      resolvers: [ElementPlusResolver({ importStyle: 'sass' })],
    }),
  ],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  css: {
    preprocessorOptions: {
      scss: {
        // Element Plus ships a newer Sass module API; silence the legacy @import deprecation.
        silenceDeprecations: ['legacy-js-api', 'import', 'global-builtin', 'color-functions'],
        api: 'modern-compiler',
      },
    },
  },
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      // Local dev only. In production Nginx serves the SPA and the API on one origin (:18000).
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
  build: {
    target: 'es2022',
    sourcemap: true,
    chunkSizeWarningLimit: 1200,
    // Vite 8 builds with Rolldown. The object form of `output.manualChunks` was REMOVED
    // and the function form is deprecated; `codeSplitting.groups` is the supported API.
    // https://vite.dev/guide/migration (Removed object form build.rollupOptions.output.manualChunks)
    rolldownOptions: {
      output: {
        codeSplitting: {
          groups: [
            {
              name: 'charts',
              test: /[\\/]node_modules[\\/](echarts|zrender)[\\/]/,
              priority: 30,
            },
            {
              name: 'element',
              test: /[\\/]node_modules[\\/]element-plus[\\/]/,
              priority: 20,
            },
            {
              name: 'vendor',
              test: /[\\/]node_modules[\\/](vue|vue-router|pinia|@vue|axios|dayjs)[\\/]/,
              priority: 10,
            },
          ],
        },
      },
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    include: ['src/**/*.spec.ts', 'tests/**/*.spec.ts'],
    exclude: ['node_modules', 'dist', 'e2e/**'],
    setupFiles: ['./tests/setup.ts'],
  },
})
