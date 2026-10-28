import { AgentApiConfig } from '@poc/api-client';

/**
 * `mock` runs the in-memory mock of contracts/agent-api.yaml in the browser, so the console
 * works without a backend. Switch `mode` to `http` to use the real agent service.
 */
export const environment: { api: AgentApiConfig; customerAppUrl: string } = {
  api: {
    mode: 'mock',
    baseUrl: 'http://localhost:8000',
  },
  /** For the "watch it live" link after Simulate customer. */
  customerAppUrl: 'http://localhost:4300',
};
