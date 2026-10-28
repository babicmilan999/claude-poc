import { AgentApiConfig } from '@poc/api-client';

/**
 * `--configuration real`: the real agent service. The browser calls it directly (CORS:
 * the agent service allows http://localhost:4300 via `CORS_ORIGINS`). A `config.json`
 * next to index.html can override `apiBaseUrl` at run time (the Docker image writes it).
 */
export const environment: { api: AgentApiConfig; runtimeConfig: boolean } = {
  api: {
    mode: 'http',
    baseUrl: 'http://localhost:8000',
  },
  runtimeConfig: true,
};
