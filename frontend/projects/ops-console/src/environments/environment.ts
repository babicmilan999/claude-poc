import { AgentApiConfig } from '@poc/api-client';

/**
 * Default build (dev server, `npm run build`, tests): the in-memory mock of
 * contracts/agent-api.yaml runs in the browser, so the console works without a backend.
 * `--configuration real` swaps in environment.real.ts (the real agent service).
 */
export const environment: { api: AgentApiConfig; customerAppUrl: string; runtimeConfig: boolean } = {
  api: {
    mode: 'mock',
    baseUrl: 'http://localhost:8000',
  },
  /** For the "watch it live" link after Simulate customer. */
  customerAppUrl: 'http://localhost:4300',
  /** Read `config.json` at startup (real builds only). */
  runtimeConfig: false,
};
