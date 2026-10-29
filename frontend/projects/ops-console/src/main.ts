import { bootstrapApplication } from '@angular/platform-browser';
import { applyRuntimeConfig, fetchRuntimeConfig } from '@poc/api-client';
import { opsConsoleConfig } from './app/app.config';
import { App } from './app/app';
import { environment } from './environments/environment';

async function start(): Promise<void> {
  const runtime = environment.runtimeConfig ? await fetchRuntimeConfig((url, init) => fetch(url, init)) : {};
  const api = applyRuntimeConfig(environment.api, runtime);
  await bootstrapApplication(App, opsConsoleConfig(api, runtime.customerAppUrl ?? environment.customerAppUrl));
}

start().catch((err) => console.error(err));
