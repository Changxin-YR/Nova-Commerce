/**
 * Global Vitest setup. Runs before every spec file.
 * Keep this file free of app imports: it must not pull in router/store singletons.
 */
import { config } from '@vue/test-utils'

config.global.stubs = {
  teleport: true,
  transition: false,
  'transition-group': false,
}
