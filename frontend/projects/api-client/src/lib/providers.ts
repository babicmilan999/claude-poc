import { EnvironmentProviders, InjectionToken, inject, makeEnvironmentProviders } from '@angular/core';
import { ApiTransport, CustomerApi, OpsApi, SharedApi } from './client';
import { MockAgentServer, MockServerOptions } from './mock/mock-server';

export interface AgentApiConfig {
  /** `mock`: the in-memory mock of the contract; `http`: the real agent service. */
  mode: 'mock' | 'http';
  /** Agent service base URL (http mode), e.g. `http://localhost:8000`. */
  baseUrl: string;
  /** Mock options (mock mode). */
  mock?: MockServerOptions;
  /** Use this mock instance instead of creating one (tests). */
  mockServer?: MockAgentServer;
  /** Wait before reopening a dropped stream, in ms. */
  reconnectDelayMs?: number;
}

/** The logged-in customer for `X-Customer-Id`; provided by the customer app. */
export const CURRENT_CUSTOMER_ID = new InjectionToken<() => string | null>('CURRENT_CUSTOMER_ID');

/** The mock server in mock mode, `null` against the real API. */
export const MOCK_AGENT_SERVER = new InjectionToken<MockAgentServer | null>('MOCK_AGENT_SERVER');

export function provideAgentApi(config: AgentApiConfig): EnvironmentProviders {
  return makeEnvironmentProviders([
    {
      provide: MOCK_AGENT_SERVER,
      useFactory: () => (config.mode === 'mock' ? (config.mockServer ?? new MockAgentServer(config.mock)) : null),
    },
    {
      provide: ApiTransport,
      useFactory: () => {
        const mock = inject(MOCK_AGENT_SERVER);
        return new ApiTransport({
          baseUrl: mock ? '' : config.baseUrl,
          fetch: mock ? mock.fetch : (input, init) => globalThis.fetch(input, init),
          reconnectDelayMs: config.reconnectDelayMs,
        });
      },
    },
    { provide: OpsApi, useFactory: () => new OpsApi(inject(ApiTransport)) },
    { provide: SharedApi, useFactory: () => new SharedApi(inject(ApiTransport)) },
    {
      provide: CustomerApi,
      useFactory: () => {
        const customerId = inject(CURRENT_CUSTOMER_ID, { optional: true }) ?? (() => null);
        return new CustomerApi(inject(ApiTransport), customerId);
      },
    },
  ]);
}
