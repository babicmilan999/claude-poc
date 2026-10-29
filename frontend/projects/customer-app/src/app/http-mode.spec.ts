// The app in `http` mode (as built with `--configuration real`), against a fake `fetch`
// that forwards to the mock server unless a test intercepts the request. No network.
import { provideRouter, withComponentInputBinding } from '@angular/router';
import { RouterTestingHarness } from '@angular/router/testing';
import { CustomerSummary, FetchFn, MockAgentServer, SharedApi } from '@poc/api-client';
import { customerAppProviders } from './app.config';
import { routes } from './app.routes';
import { CustomerSession } from './session';
import { TestBed, all, el, mockServer, text, typeInto, waitFor } from './testing';

const BASE = 'http://agent.test:8000';

const problem = (status: number, title: string) =>
  new Response(JSON.stringify({ title, status }), { status, headers: { 'Content-Type': 'application/problem+json' } });

/** Passes the first `keep` chunks of a stream through, then drops the connection. */
function dropAfter(res: Response, keep: number): Response {
  const reader = res.body!.getReader();
  let n = 0;
  const body = new ReadableStream<Uint8Array>({
    async pull(controller) {
      if (n++ >= keep) {
        void reader.cancel();
        controller.error(new TypeError('network connection was lost'));
        return;
      }
      const { done, value } = await reader.read();
      if (done) controller.close();
      else controller.enqueue(value);
    },
  });
  return new Response(body, { status: res.status, headers: res.headers });
}

describe('customer app against the real API (http mode)', () => {
  let server: MockAgentServer;
  let harness: RouterTestingHarness;
  let calls: string[];
  let intercept: ((method: string, url: string, init?: RequestInit) => Promise<Response | null> | Response | null) | null;

  beforeEach(async () => {
    sessionStorage.clear();
    server = mockServer();
    calls = [];
    intercept = null;
    const fetchFn: FetchFn = async (url, init) => {
      const method = init?.method ?? 'GET';
      calls.push(`${method} ${url}`);
      const custom = await intercept?.(method, url, init);
      if (custom) return custom;
      return server.fetch(url, init);
    };
    TestBed.configureTestingModule({
      providers: [
        provideRouter(routes, withComponentInputBinding()),
        ...customerAppProviders({ mode: 'http', baseUrl: BASE, fetch: fetchFn, reconnectDelayMs: 0 }),
      ],
    });
    harness = await RouterTestingHarness.create();
  });

  afterEach(() => server.settle());

  async function customer(name: string): Promise<CustomerSummary> {
    const list = await TestBed.inject(SharedApi).listCustomers();
    return list.find((c) => c.name.startsWith(name))!;
  }

  async function send(message: string) {
    harness.fixture.detectChanges();
    typeInto(el<HTMLInputElement>(harness.fixture, 'input[name=draft]'), message);
    el<HTMLFormElement>(harness.fixture, 'form').dispatchEvent(new Event('submit'));
  }

  it('calls the configured base URL; an article source opens the help placeholder', async () => {
    TestBed.inject(CustomerSession).logIn(await customer('Alice'));
    await harness.navigateByUrl('/chat');
    await send('Why was my card declined?');
    await waitFor(harness.fixture, () => all(harness.fixture, '.thumb').length === 2);
    expect(calls).toContain(`POST ${BASE}/conversations`);
    expect(calls.some((c) => /^POST http:\/\/agent\.test:8000\/conversations\/[0-9a-f-]+\/messages$/.test(c))).toBe(true);

    const link = el<HTMLAnchorElement>(harness.fixture, 'a.source');
    expect(link.getAttribute('href')).toBe('/help/why-cards-get-declined');
    expect(link.getAttribute('target')).toBeNull(); // routed inside the app, not a new tab
    link.click();
    await waitFor(harness.fixture, () => text(harness.fixture).includes('Help article'));
    expect(text(harness.fixture)).toContain('Why cards get declined');
    expect(text(harness.fixture)).toContain('why-cards-get-declined');
  });

  it('401 (customer unknown to the agent service) logs out and asks to log in again', async () => {
    TestBed.inject(CustomerSession).logIn({ id: '00000000-0000-4000-8000-000000000000', name: 'Ghost' });
    await harness.navigateByUrl('/account');
    await waitFor(harness.fixture, () => text(harness.fixture).includes('does not know that customer'));
    expect(TestBed.inject(CustomerSession).customer()).toBeNull();
    expect(text(harness.fixture)).toContain('Log in as');
    await waitFor(harness.fixture, () => all(harness.fixture, '.list-button').length >= 6);
  });

  it('502 on the account view shows a message and Try again reloads it', async () => {
    TestBed.inject(CustomerSession).logIn(await customer('Alice'));
    let failures = 1;
    intercept = (method, url) =>
      method === 'GET' && url.endsWith('/me/overview') && failures-- > 0 ? problem(502, 'Banking service unavailable') : null;
    await harness.navigateByUrl('/account');
    await waitFor(harness.fixture, () => text(harness.fixture).includes('not available right now'));
    el<HTMLButtonElement>(harness.fixture, '.error button').click();
    await waitFor(harness.fixture, () => text(harness.fixture).includes('Card ending'));
    expect(text(harness.fixture)).not.toContain('not available right now');
  });

  it('a chat stream that drops mid-answer stops the caret, shows the error and reloads the conversation', async () => {
    TestBed.inject(CustomerSession).logIn(await customer('Alice'));
    intercept = (method, url, init) =>
      method === 'POST' && url.endsWith('/messages') ? server.fetch(url, init).then((res) => dropAfter(res, 3)) : null;
    await harness.navigateByUrl('/chat');
    await send('Why was my card declined?');
    await waitFor(harness.fixture, () => text(harness.fixture).includes('Cannot reach the agent service'));
    await waitFor(harness.fixture, () => calls.some((c) => /^GET .*\/conversations\/[0-9a-f-]+$/.test(c)));
    harness.fixture.detectChanges();
    expect(all(harness.fixture, '.caret')).toHaveLength(0);
    expect(el<HTMLInputElement>(harness.fixture, 'input[name=draft]').disabled).toBe(false);
  });

  it('409 on send (conversation closed meanwhile) shows why and reloads it as closed', async () => {
    TestBed.inject(CustomerSession).logIn(await customer('Alice'));
    await harness.navigateByUrl('/chat');
    await send('Why was my card declined?');
    await waitFor(harness.fixture, () => all(harness.fixture, '.thumb').length === 2);

    // The server closed the conversation (e.g. abandoned) while the page still shows it open.
    intercept = async (method, url, init) => {
      if (method === 'POST' && url.endsWith('/messages')) return problem(409, 'Conversation is closed');
      if (method === 'GET' && /\/conversations\/[0-9a-f-]+$/.test(url)) {
        const detail = await (await server.fetch(url, init)).json();
        return new Response(JSON.stringify({ ...detail, status: 'closed', outcome: 'abandoned' }), {
          headers: { 'Content-Type': 'application/json' },
        });
      }
      return null;
    };
    await send('And my other card?');
    await waitFor(harness.fixture, () => text(harness.fixture).includes('This conversation is closed'));
    expect(text(harness.fixture)).toContain('Conversation is closed');
  });
});
