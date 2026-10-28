import { ApplicationConfig, inject, provideBrowserGlobalErrorListeners } from '@angular/core';
import { provideRouter, withComponentInputBinding } from '@angular/router';
import { AgentApiConfig, CURRENT_CUSTOMER_ID, provideAgentApi } from '@poc/api-client';
import { environment } from '../environments/environment';
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
  ];
}

export const appConfig: ApplicationConfig = {
  providers: [
    provideBrowserGlobalErrorListeners(),
    provideRouter(routes, withComponentInputBinding()),
    ...customerAppProviders(environment.api),
  ],
};
