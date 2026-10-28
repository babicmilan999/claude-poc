import { ApplicationConfig, InjectionToken, provideBrowserGlobalErrorListeners } from '@angular/core';
import { provideRouter, withComponentInputBinding } from '@angular/router';
import { AgentApiConfig, provideAgentApi } from '@poc/api-client';
import { environment } from '../environments/environment';
import { routes } from './app.routes';

/** Base URL of the customer app, for watch-mode links. */
export const CUSTOMER_APP_URL = new InjectionToken<string>('CUSTOMER_APP_URL');

export function opsConsoleProviders(api: AgentApiConfig, customerAppUrl = environment.customerAppUrl) {
  return [provideAgentApi(api), { provide: CUSTOMER_APP_URL, useValue: customerAppUrl }];
}

export const appConfig: ApplicationConfig = {
  providers: [
    provideBrowserGlobalErrorListeners(),
    provideRouter(routes, withComponentInputBinding()),
    ...opsConsoleProviders(environment.api),
  ],
};
