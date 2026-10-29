import { EnvironmentProviders, InjectionToken, inject, makeEnvironmentProviders } from '@angular/core';
import { ApiTransport, CustomerApi, OpsApi, SharedApi } from './client';
import { ApiError, FetchFn } from './sse';
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
  /** `fetch` for http mode; default `globalThis.fetch` (tests pass a fake). */
  fetch?: FetchFn;
  /** Wait before reopening a dropped stream, in ms. */
  reconnectDelayMs?: number;
}

/** The logged-in customer for `X-Customer-Id`; provided by the customer app. */
export const CURRENT_CUSTOMER_ID = new InjectionToken<() => string | null>('CURRENT_CUSTOMER_ID');

/**
 * Called when a customer endpoint answers 401 (unknown `X-Customer-Id`); the customer
 * app logs out and returns to "log in as".
 */
export const ON_CUSTOMER_UNAUTHORIZED = new InjectionToken<(err: ApiError) => void>('ON_CUSTOMER_UNAUTHORIZED');

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
          fetch: mock ? mock.fetch : (config.fetch ?? ((input, init) => globalThis.fetch(input, init))),
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
        const onUnauthorized = inject(ON_CUSTOMER_UNAUTHORIZED, { optional: true }) ?? undefined;
        return new CustomerApi(inject(ApiTransport), customerId, onUnauthorized);
      },
    },
  ]);
}
