import { firstValueFrom, take, toArray } from 'rxjs';
import { ApiTransport, CustomerApi, OpsApi } from './client';
import { FetchFn } from './sse';

function recorder(respond: (url: string, init?: RequestInit) => Response) {
  const calls: { url: string; init?: RequestInit }[] = [];
  const fetchFn: FetchFn = async (url, init) => {
    calls.push({ url, init });
    return respond(url, init);
  };
  return { calls, fetchFn };
}

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });

describe('API client', () => {
  it('sends the logged-in customer as X-Customer-Id', async () => {
    const { calls, fetchFn } = recorder(() => json({ customer: {}, accounts: [], cards: [], recent_transactions: [] }));
    const api = new CustomerApi(new ApiTransport({ baseUrl: 'http://agent:8000/', fetch: fetchFn }), () => 'cust-1');
    await api.getMyOverview();
    expect(calls[0].url).toBe('http://agent:8000/me/overview');
    expect(new Headers(calls[0].init?.headers).get('X-Customer-Id')).toBe('cust-1');
  });

  it('encodes array query parameters as form, explode=false and skips empty ones', async () => {
    const { calls, fetchFn } = recorder(() => json({ items: [], total: 0 }));
    const ops = new OpsApi(new ApiTransport({ baseUrl: '', fetch: fetchFn }));
    await ops.listCases({ status: ['new', 'investigating'], source: undefined, limit: 10 });
    expect(calls[0].url).toBe('/cases?status=new%2Cinvestigating&limit=10');
  });

  it('posts JSON bodies', async () => {
    const { calls, fetchFn } = recorder(() => json({}, 202));
    const ops = new OpsApi(new ApiTransport({ baseUrl: '', fetch: fetchFn }));
    await ops.submitFeedback('inv-1', { verdict: 'rejected', hint: 'check limits' });
    expect(calls[0].init?.method).toBe('POST');
    expect(new Headers(calls[0].init?.headers).get('Content-Type')).toBe('application/json');
    expect(JSON.parse(calls[0].init?.body as string)).toEqual({ verdict: 'rejected', hint: 'check limits' });
  });

  it('reopens the case queue stream after it closes, and asks for a reload', async () => {
    let n = 0;
    const { calls, fetchFn } = recorder(() => {
      n++;
      const body = `event: status_changed\ndata: {"id":"${n}","type":"status_changed","created_at":"2026-10-28T10:00:00Z","case_id":"c","status":"investigating"}\n\n`;
      return new Response(body, { headers: { 'Content-Type': 'text/event-stream' } });
    });
    const ops = new OpsApi(new ApiTransport({ baseUrl: '', fetch: fetchFn, reconnectDelayMs: 0 }));
    let reloads = 0;
    const events = await firstValueFrom(ops.caseEvents(() => reloads++).pipe(take(2), toArray()));
    expect(events.map((e) => e.id)).toEqual(['1', '2']);
    expect(calls).toHaveLength(2);
    expect(reloads).toBe(1);
  });
});
