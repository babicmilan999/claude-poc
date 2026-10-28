import { AgentApiConfig } from '@poc/api-client';

/**
 * `mock` runs the in-memory mock of contracts/agent-api.yaml in the browser, so the app
 * works without a backend. Switch `mode` to `http` to use the real agent service.
 */
export const environment: { api: AgentApiConfig } = {
  api: {
    mode: 'mock',
    baseUrl: 'http://localhost:8000',
  },
};
