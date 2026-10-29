import { vi } from 'vitest';
import { endChatStream, EMPTY_CHAT } from './chat-state';
import { applyRuntimeConfig, fetchRuntimeConfig, parseRuntimeConfig } from './runtime-config';
import { sourceLink } from './source-link';
import { FetchFn } from './sse';

describe('runtime config', () => {
  const respond =
    (res: Response | Error): FetchFn =>
    async () => {
      if (res instanceof Error) throw res;
      return res;
    };

  it('reads and validates config.json', async () => {
    const body = { apiBaseUrl: ' http://agent.example ', apiMode: 'http', customerAppUrl: 'http://c', extra: 1, };
    const cfg = await fetchRuntimeConfig(respond(new Response(JSON.stringify(body), { headers: { 'Content-Type': 'application/json' } })));
    expect(cfg).toEqual({ apiBaseUrl: 'http://agent.example', apiMode: 'http', customerAppUrl: 'http://c' });
  });

  it('ignores a missing file, an HTML fallback and network errors', async () => {
    expect(await fetchRuntimeConfig(respond(new Response('', { status: 404 })))).toEqual({});
    expect(
      await fetchRuntimeConfig(respond(new Response('<!doctype html>', { headers: { 'Content-Type': 'text/html' } }))),
    ).toEqual({});
    expect(await fetchRuntimeConfig(respond(new TypeError('offline')))).toEqual({});
  });

  it('accepts real as an alias for http, like the Docker build arg', () => {
    expect(parseRuntimeConfig({ apiMode: 'real' })).toEqual({ apiMode: 'http' });
    expect(parseRuntimeConfig({ apiMode: ' Mock ' })).toEqual({ apiMode: 'mock' });
  });

  it('drops fields with the wrong type or value, warning about an unknown apiMode', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined);
    expect(parseRuntimeConfig({ apiMode: 'grpc', apiBaseUrl: 8000, customerAppUrl: ' ' })).toEqual({});
    expect(warn).toHaveBeenCalledTimes(1);
    expect(String(warn.mock.calls[0][0])).toContain('grpc');
    warn.mockRestore();
    expect(parseRuntimeConfig(null)).toEqual({});
    // An empty base URL is valid: same origin (behind a reverse proxy).
    expect(parseRuntimeConfig({ apiBaseUrl: '' })).toEqual({ apiBaseUrl: '' });
  });

  it('overrides the build settings', () => {
    const build = { mode: 'http' as const, baseUrl: 'http://localhost:8000', reconnectDelayMs: 5 };
    expect(applyRuntimeConfig(build, {})).toEqual(build);
    expect(applyRuntimeConfig(build, { apiBaseUrl: 'http://agent:9000' })).toEqual({ ...build, baseUrl: 'http://agent:9000' });
  });
});

describe('sourceLink', () => {
  it('routes same-origin article paths inside the app (default /help/<slug>)', () => {
    expect(sourceLink({ kind: 'article', title: 'T', url: '/help/why-cards-get-declined' })).toEqual({
      kind: 'internal',
      path: '/help/why-cards-get-declined',
    });
  });

  it('opens absolute URLs as external links', () => {
    expect(sourceLink({ kind: 'article', title: 'T', url: 'https://help.example/a' })).toEqual({
      kind: 'external',
      href: 'https://help.example/a',
    });
    expect(sourceLink({ kind: 'article', title: 'T', url: '//evil.example/a', article_id: 'a' })).toEqual({
      kind: 'internal',
      path: '/help/a',
    });
  });

  it('falls back to the slug, and shows account data and unusable URLs as plain chips', () => {
    expect(sourceLink({ kind: 'article', title: 'T', article_id: 'fees' })).toEqual({ kind: 'internal', path: '/help/fees' });
    expect(sourceLink({ kind: 'article', title: 'T', url: 'javascript:alert(1)' })).toEqual({ kind: 'none' });
    expect(sourceLink({ kind: 'account_data', title: 'Your cards', tool: 'get_my_cards', url: '/x' })).toEqual({ kind: 'none' });
  });
});

describe('endChatStream', () => {
  it('stops messages left streaming and reports it', () => {
    const msg = { id: 2, conversation_id: 'c', role: 'assistant' as const, content: 'Half', sources: [], created_at: '' };
    const { state, incomplete } = endChatStream({ ...EMPTY_CHAT, messages: [{ ...msg, streaming: true }] });
    expect(incomplete).toBe(true);
    expect(state.messages[0].streaming).toBe(false);
    const done = { ...EMPTY_CHAT, messages: [{ ...msg, streaming: false }] };
    expect(endChatStream(done)).toEqual({ state: done, incomplete: false });
  });
});
