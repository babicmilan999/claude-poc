import type { AgentApiConfig } from './providers';
import { FetchFn } from './sse';

/**
 * Settings a deployed build can override at run time, from a `config.json` next to
 * `index.html` (the Docker image writes it from environment variables). Every field is
 * optional; missing or invalid ones keep the build's value.
 */
export interface RuntimeConfig {
  /** `mock` or `http`. */
  apiMode?: 'mock' | 'http';
  /** Agent service base URL, e.g. `http://localhost:8000`, or `` for same origin. */
  apiBaseUrl?: string;
  /** Ops console only: the customer app, for watch-mode links. */
  customerAppUrl?: string;
}

/**
 * Fetches and validates `config.json`. Returns `{}` when there is none (404), when the
 * server answered with something else (a dev server's `index.html` fallback), or on any
 * error, so a missing file never stops the app from starting.
 */
export async function fetchRuntimeConfig(fetchFn: FetchFn, url = 'config.json'): Promise<RuntimeConfig> {
  try {
    const res = await fetchFn(url, { cache: 'no-store', headers: { Accept: 'application/json' } });
    if (!res.ok) return {};
    if (!(res.headers.get('Content-Type') ?? '').includes('json')) return {};
    return parseRuntimeConfig(await res.json());
  } catch {
    return {};
  }
}

/** Keeps only the known fields with the right types. */
export function parseRuntimeConfig(raw: unknown): RuntimeConfig {
  if (!raw || typeof raw !== 'object') return {};
  const input = raw as Record<string, unknown>;
  const out: RuntimeConfig = {};
  if (input['apiMode'] === 'mock' || input['apiMode'] === 'http') out.apiMode = input['apiMode'];
  if (typeof input['apiBaseUrl'] === 'string') out.apiBaseUrl = input['apiBaseUrl'].trim();
  if (typeof input['customerAppUrl'] === 'string' && input['customerAppUrl'].trim()) {
    out.customerAppUrl = input['customerAppUrl'].trim();
  }
  return out;
}

/** The build's API settings with the run-time overrides applied. */
export function applyRuntimeConfig(api: AgentApiConfig, runtime: RuntimeConfig): AgentApiConfig {
  return {
    ...api,
    mode: runtime.apiMode ?? api.mode,
    baseUrl: runtime.apiBaseUrl ?? api.baseUrl,
  };
}
