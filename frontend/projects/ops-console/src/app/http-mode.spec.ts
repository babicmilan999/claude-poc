// The console in `http` mode (as built with `--configuration real`), against a fake
// `fetch` that forwards to the mock server unless a test intercepts the request. No network.
import { provideRouter, withComponentInputBinding } from '@angular/router';
import { RouterTestingHarness } from '@angular/router/testing';
import { FetchFn, MockAgentServer, OpsApi } from '@poc/api-client';
import { opsConsoleProviders } from './app.config';
import { routes } from './app.routes';
import { TestBed, all, el, mockServer, text, typeInto, waitFor } from './testing';

const BASE = 'http://agent.test:8000';

interface Call {
  method: string;
  url: string;
  headers: Headers;
}

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

describe('ops console against the real API (http mode)', () => {
  let server: MockAgentServer;
  let harness: RouterTestingHarness;
  let calls: Call[];
  let intercept: ((call: Call, init?: RequestInit) => Promise<Response | null> | Response | null) | null;

  beforeEach(async () => {
    server = mockServer();
    calls = [];
    intercept = null;
    const fetchFn: FetchFn = async (url, init) => {
      const call = { method: init?.method ?? 'GET', url, headers: new Headers(init?.headers) };
      calls.push(call);
      const custom = await intercept?.(call, init);
      if (custom) return custom;
      return server.fetch(url, init);
    };
    TestBed.configureTestingModule({
      providers: [
        provideRouter(routes, withComponentInputBinding()),
        ...opsConsoleProviders({ mode: 'http', baseUrl: BASE, fetch: fetchFn, reconnectDelayMs: 0 }, 'http://customer.test'),
      ],
    });
    harness = await RouterTestingHarness.create();
  });

  afterEach(() => server.settle());

  async function caseOf(name: string) {
    const page = await TestBed.inject(OpsApi).listCases();
    return page.items.find((c) => c.customer_name?.startsWith(name))!;
  }

  it('loads the queue from the configured base URL', async () => {
    await harness.navigateByUrl('/cases');
    await waitFor(harness.fixture, () => all(harness.fixture, 'tbody tr[data-case-id]').length === 3);
    expect(calls.some((c) => c.method === 'GET' && c.url === `${BASE}/cases`)).toBe(true);
    expect(calls.some((c) => c.url === `${BASE}/cases/events` && c.headers.get('Accept') === 'text/event-stream')).toBe(true);
  });

  it('409 on Re-investigate shows the reason and reloads the case', async () => {
    const ben = await caseOf('Ben');
    await harness.navigateByUrl(`/cases/${ben.id}`);
    await waitFor(harness.fixture, () => text(harness.fixture).includes('Submit review'));
    intercept = (call) =>
      call.method === 'POST' && call.url.endsWith('/reinvestigate')
        ? new Response(
            JSON.stringify({ title: 'Conflict', status: 409, detail: 'An investigation is already queued or running' }),
            { status: 409, headers: { 'Content-Type': 'application/problem+json' } },
          )
        : null;
    const before = calls.filter((c) => c.url === `${BASE}/cases/${ben.id}`).length;
    el<HTMLButtonElement>(harness.fixture, 'button.reinvestigate').click();
    await waitFor(harness.fixture, () => text(harness.fixture).includes('Not started: An investigation is already queued or running'));
    expect(calls.filter((c) => c.url === `${BASE}/cases/${ben.id}`).length).toBe(before + 1);
  });

  it('a 404 from the simulation endpoints says simulation is off', async () => {
    intercept = (call) =>
      call.url.startsWith(`${BASE}/simulation/`)
        ? new Response(JSON.stringify({ detail: 'Not Found' }), { status: 404, headers: { 'Content-Type': 'application/json' } })
        : null;
    await harness.navigateByUrl('/cases');
    await waitFor(harness.fixture, () => all(harness.fixture, 'tbody tr[data-case-id]').length === 3);
    el<HTMLButtonElement>(harness.fixture, 'button.sim-email').click();
    await waitFor(harness.fixture, () => text(harness.fixture).includes('Simulation is turned off'));
  });

  it('the live timeline resumes with Last-Event-ID after a dropped connection', async () => {
    const ben = await caseOf('Ben');
    await harness.navigateByUrl(`/cases/${ben.id}`);
    await waitFor(harness.fixture, () => text(harness.fixture).includes('Submit review'));

    let streams = 0;
    intercept = (call, init) => {
      if (!(call.url.endsWith('/events') && call.url.includes('/investigations/') && call.headers.get('Accept') === 'text/event-stream')) {
        return null;
      }
      // Drop the first live connection after two events.
      return streams++ === 0 ? server.fetch(call.url, init).then((res) => dropAfter(res, 2)) : null;
    };

    const reject = all<HTMLInputElement>(harness.fixture, 'input[name=verdict]').find((r) => r.value === 'rejected')!;
    reject.click();
    reject.dispatchEvent(new Event('change'));
    harness.fixture.detectChanges();
    await harness.fixture.whenStable();
    typeInto(el<HTMLTextAreaElement>(harness.fixture, '#hint'), 'Check the daily limit');
    harness.fixture.detectChanges();
    el<HTMLButtonElement>(harness.fixture, 'button[type=submit]').click();

    await waitFor(harness.fixture, () => text(harness.fixture).includes('Daily payment limit exceeded') && text(harness.fixture).includes('Submit review'));
    const live = calls.filter((c) => c.url.includes('/investigations/') && c.headers.get('Accept') === 'text/event-stream');
    expect(live.length).toBeGreaterThanOrEqual(2);
    const first = Number(live[0].headers.get('Last-Event-ID'));
    const second = Number(live[1].headers.get('Last-Event-ID'));
    expect(first).toBeGreaterThan(0); // resumes after the stored timeline already shown
    expect(second).toBeGreaterThan(first); // and after what the dropped connection delivered
    // No event shown twice after the replay.
    const ids = all(harness.fixture, '.timeline li').map((li) => li.textContent);
    expect(ids.filter((t) => t?.includes('Daily payment limit exceeded')).length).toBeLessThanOrEqual(1);
  });
});
