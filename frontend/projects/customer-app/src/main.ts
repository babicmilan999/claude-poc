import { bootstrapApplication } from '@angular/platform-browser';
import { applyRuntimeConfig, fetchRuntimeConfig } from '@poc/api-client';
import { customerAppConfig } from './app/app.config';
import { App } from './app/app';
import { environment } from './environments/environment';

async function start(): Promise<void> {
  const runtime = environment.runtimeConfig ? await fetchRuntimeConfig((url, init) => fetch(url, init)) : {};
  await bootstrapApplication(App, customerAppConfig(applyRuntimeConfig(environment.api, runtime)));
}

start().catch((err) => console.error(err));
