import { ApplicationConfig, Injector, inject, provideBrowserGlobalErrorListeners } from '@angular/core';
import { Router, provideRouter, withComponentInputBinding } from '@angular/router';
import { AgentApiConfig, CURRENT_CUSTOMER_ID, ON_CUSTOMER_UNAUTHORIZED, provideAgentApi } from '@poc/api-client';
import { routes } from './app.routes';
import { CustomerSession } from './session';

export function customerAppProviders(api: AgentApiConfig) {
  return [
    provideAgentApi(api),
    {
      provide: CURRENT_CUSTOMER_ID,
      useFactory: () => {
        const session = inject(CustomerSession);
        return () => session.customer()?.id ?? null;
      },
    },
    {
      // 401: the agent service does not know this customer (e.g. its database was reset
      // since "log in as"). Log out and pick again.
      provide: ON_CUSTOMER_UNAUTHORIZED,
      useFactory: () => {
        const session = inject(CustomerSession);
        const injector = inject(Injector);
        return () => {
          if (!session.customer()) return;
          session.logOut('The agent service does not know that customer any more. Please log in again.');
          void injector.get(Router).navigateByUrl('/login');
        };
      },
    },
  ];
}

export function customerAppConfig(api: AgentApiConfig): ApplicationConfig {
  return {
    providers: [
      provideBrowserGlobalErrorListeners(),
      provideRouter(routes, withComponentInputBinding()),
      ...customerAppProviders(api),
    ],
  };
}
