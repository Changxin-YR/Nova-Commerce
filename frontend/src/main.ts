/**
 * Application entry.
 *
 * Order matters:
 *   1. Element Plus + its base styles.
 *   2. Our design tokens, so they can override Element Plus variables.
 *   3. Pinia, then the router (route guards read stores, so Pinia must be installed
 *      first).
 *   4. The permission directives (§104).
 */

import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import 'element-plus/dist/index.css'

import App from '@/App.vue'
import router from '@/router'
import { registerPermissionDirectives } from '@/directives/permission'
import '@/styles/tokens.scss'

const app = createApp(App)

app.use(createPinia())
app.use(ElementPlus, { locale: zhCn, size: 'default', zIndex: 3000 })
app.use(router)

// `v-permission` / `v-can` are UX helpers only — see src/directives/permission.ts.
registerPermissionDirectives(app)

// A global error boundary: an unexpected render/lifecycle error must be visible
// rather than leaving a blank page (and must not leak a stack trace to the user).
app.config.errorHandler = (error, _instance, info) => {
  console.error('[nova] unhandled error', info, error)
}

app.mount('#app')
