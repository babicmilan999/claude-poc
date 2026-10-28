import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vitest/config';

// The library is framework-free apart from providers.ts, so its tests (SSE client, state,
// mock server, contract conformance) run in plain Node with no browser.
export default defineConfig({
  test: {
    root: fileURLToPath(new URL('.', import.meta.url)),
    environment: 'node',
    include: ['src/**/*.spec.ts'],
    globals: true,
  },
});
