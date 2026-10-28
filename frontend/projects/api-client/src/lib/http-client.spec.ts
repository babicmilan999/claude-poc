// The `http` code path against a fake `fetch`: status codes, problem+json, SSE parsing
// over chunk boundaries, reconnects. No network, no mock server.
import { firstValueFrom, take, toArray } from 'rxjs';
import { ApiTransport, CustomerApi, OpsApi, SharedApi } from './client';
import { ApiError, FetchFn, NetworkError } from './sse';

const BASE = 'http://agent.test:8000';

interface Call {
  url: string;
  method: string;
  headers: Headers;
}

/** A fake fetch answering from a queue of handlers; records every call. */
function fakeFetch(...handlers: ((call: Call) => Response | Promise<Response>)[]) {
  const calls: Call[] = [];
  const fetchFn: FetchFn = async (url, init) => {
    const call = { url, method: init?.method ?? 'GET', headers: new Headers(init?.headers) };
    calls.push(call);
    const handler = handlers.length > 1 ? handlers.shift()! : handlers[0];
    if (!handler) throw new Error(`unexpected call ${url}`);
    return handler(call);
  };
  return { calls, fetchFn };
}

const problem = (status: number, title: string, detail?: string) =>
  new Response(JSON.stringify({ title, status, ...(detail ? { detail } : {}) }), {
    status,
    headers: { 'Content-Type': 'application/problem+json' },
  });

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });

/** A response body that delivers `chunks` (strings, or an Error to drop the connection). */
function sse(chunks: (string | Error)[]): Response {
  const encoder = new TextEncoder();
  let i = 0;
  const body = new ReadableStream<Uint8Array>({
    pull(controller) {
      if (i >= chunks.length) {
        controller.close();
        return;
      }
      const chunk = chunks[i++];
      if (chunk instanceof Error) controller.error(chunk);
      else controller.enqueue(encoder.encode(chunk));
    },
  });
  return new Response(body, { headers: { 'Content-Type': 'text/event-stream' } });
}

function invEvent(id: number, type: string, payload: object): string {
  const data = JSON.stringify({ id, investigation_id: 'inv-1', type, created_at: '2026-10-28T10:00:00Z', payload });
  return `id: ${id}\nevent: ${type}\ndata: ${data}\n\n`;
}

function transport(fetchFn: FetchFn) {
  return new ApiTransport({ baseUrl: BASE, fetch: fetchFn, reconnectDelayMs: 0 });
}

describe('http client: errors', () => {
  it('turns a 401 problem+json into ApiError and tells the app (unknown X-Customer-Id)', async () => {
    const { fetchFn } = fakeFetch(() => problem(401, 'Unknown customer', 'Set X-Customer-Id to a known customer'));
    const seen: ApiError[] = [];
    const api = new CustomerApi(transport(fetchFn), () => 'gone', (err) => seen.push(err));
    const err = await api.getMyOverview().catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(401);
    expect(err.problem).toEqual({ title: 'Unknown customer', status: 401, detail: 'Set X-Customer-Id to a known customer' });
    expect(err.message).toBe('Unknown customer: Set X-Customer-Id to a known customer');
    expect(seen).toEqual([err]);
  });

  it('does not call the 401 hook for other statuses (502 on /me/overview)', async () => {
    const { fetchFn } = fakeFetch(() => problem(502, 'Banking service unavailable'));
    let called = 0;
    const api = new CustomerApi(transport(fetchFn), () => 'c1', () => called++);
    const err = await api.getMyOverview().catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(502);
    expect(err.message).toBe('Banking service unavailable');
    expect(called).toBe(0);
  });

  it('calls the 401 hook on createConversation and rateMessage too', async () => {
    const { fetchFn } = fakeFetch(() => problem(401, 'Unknown customer'));
    let called = 0;
    const api = new CustomerApi(transport(fetchFn), () => 'gone', () => called++);
    await api.createConversation().catch(() => undefined);
    await api.rateMessage('conv', 3, 'up').catch(() => undefined);
    expect(called).toBe(2);
  });

  it('keeps the problem detail of a 409 on Re-investigate', async () => {
    const { calls, fetchFn } = fakeFetch(() => problem(409, 'Conflict', 'An investigation is already queued or running'));
    const err = await new OpsApi(transport(fetchFn)).reinvestigateCase('case-1').catch((e) => e);
    expect(calls[0].method).toBe('POST');
    expect(calls[0].url).toBe(`${BASE}/cases/case-1/reinvestigate`);
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(409);
    expect(err.problem.detail).toBe('An investigation is already queued or running');
  });

  it("reads FastAPI's default {detail} body (unknown route, e.g. simulation turned off)", async () => {
    const { fetchFn } = fakeFetch(() => json({ detail: 'Not Found' }, 404));
    const err = await new OpsApi(transport(fetchFn)).simulateEmail().catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(404);
    expect(err.message).toBe('Not Found');
  });

  it('falls back to a status title for a non-JSON body (a proxy error page)', async () => {
    const { fetchFn } = fakeFetch(() => new Response('<html>Bad Gateway</html>', { status: 502 }));
    const err = await new SharedApi(transport(fetchFn)).listCustomers().catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err.message).toBe('Bad gateway');
  });

  it('turns a failed fetch (service down, CORS) into NetworkError', async () => {
    const fetchFn: FetchFn = async () => {
      throw new TypeError('Failed to fetch');
    };
    const err = await new SharedApi(transport(fetchFn)).listCustomers().catch((e) => e);
    expect(err).toBeInstanceOf(NetworkError);
    expect(err.message).toBe(`Cannot reach the agent service (${BASE}/customers)`);
    expect(err.cause).toBeInstanceOf(TypeError);
  });

  it('asks for JSON and passes after_id for the stored timeline', async () => {
    const { calls, fetchFn } = fakeFetch(() => json([]));
    await new OpsApi(transport(fetchFn)).getInvestigationEvents('inv-1', 7);
    expect(calls[0].url).toBe(`${BASE}/investigations/inv-1/events?after_id=7`);
    expect(calls[0].headers.get('Accept')).toBe('application/json');
  });
});

describe('http client: chat stream', () => {
  const created = JSON.stringify({
    id: 'e1',
    type: 'message_created',
    conversation_id: 'conv',
    created_at: '2026-10-28T10:00:00Z',
    message: { id: 1, conversation_id: 'conv', role: 'customer', content: 'Hi', sources: [], created_at: '2026-10-28T10:00:00Z' },
  });
  const token = (id: string, text: string) =>
    JSON.stringify({ id, type: 'token', conversation_id: 'conv', created_at: '2026-10-28T10:00:01Z', message_id: 2, text });

  it('parses events split anywhere across chunks (mid-field, mid-JSON, mid-CRLF, multi-byte)', async () => {
    const wire = `event: message_created\r\ndata: ${created}\r\n\r\n: keep-alive\r\n\r\nevent: token\r\ndata: ${token('e2', 'Ça va ')}\r\n\r\nevent: token\ndata: ${token('e3', 'bien')}\n\n`;
    // Cut the UTF-8 bytes into 7-byte pieces, so splits land inside characters too.
    const bytes = new TextEncoder().encode(wire);
    const pieces: Uint8Array[] = [];
    for (let i = 0; i < bytes.length; i += 7) pieces.push(bytes.slice(i, i + 7));
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        pieces.forEach((p) => controller.enqueue(p));
        controller.close();
      },
    });
    const { calls, fetchFn } = fakeFetch(() => new Response(body, { headers: { 'Content-Type': 'text/event-stream' } }));
    const api = new CustomerApi(transport(fetchFn), () => 'c1');
    const events = await firstValueFrom(api.sendMessage('conv', 'Hi').pipe(toArray()));
    expect(events.map((e) => e.type)).toEqual(['message_created', 'token', 'token']);
    expect(events.filter((e) => e.type === 'token').map((e) => (e as { text: string }).text).join('')).toBe('Ça va bien');
    expect(calls[0].method).toBe('POST');
    expect(calls[0].url).toBe(`${BASE}/conversations/conv/messages`);
    expect(calls[0].headers.get('Accept')).toBe('text/event-stream');
    expect(calls[0].headers.get('X-Customer-Id')).toBe('c1');
  });

  it('errors with ApiError on 409 (conversation closed) without reading a stream', async () => {
    const { fetchFn } = fakeFetch(() => problem(409, 'Conversation is closed'));
    const err = await firstValueFrom(new CustomerApi(transport(fetchFn), () => 'c1').sendMessage('conv', 'Hi')).catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(409);
  });

  it('calls the 401 hook when the stream request is refused', async () => {
    const { fetchFn } = fakeFetch(() => problem(401, 'Unknown customer'));
    let called = 0;
    const api = new CustomerApi(transport(fetchFn), () => 'gone', () => called++);
    await firstValueFrom(api.sendMessage('conv', 'Hi')).catch(() => undefined);
    expect(called).toBe(1);
  });

  it('errors with NetworkError when the connection drops mid-answer (not retried)', async () => {
    const { calls, fetchFn } = fakeFetch(() => sse([`event: token\ndata: ${token('e2', 'Half')}\n\n`, new TypeError('network reset')]));
    const received: string[] = [];
    const err = await new Promise((resolve) =>
      new CustomerApi(transport(fetchFn), () => 'c1').sendMessage('conv', 'Hi').subscribe({
        next: (e) => received.push(e.type),
        error: resolve,
      }),
    );
    expect(received).toEqual(['token']);
    expect(err).toBeInstanceOf(NetworkError);
    expect(calls).toHaveLength(1); // a POST is never replayed
  });
});

describe('http client: investigation timeline', () => {
  const step = (id: number) => invEvent(id, 'step_started', { node: 'triage' });
  const awaiting = (id: number) => invEvent(id, 'awaiting_review', { has_diagnosis: true });
  const investigation = (status: string) =>
    json({ id: 'inv-1', case_id: 'c', trigger: 'auto', status, queued_at: '2026-10-28T10:00:00Z', steps_used: 3, evidence: [] });

  it('reconnects with Last-Event-ID after a drop and completes after the final event', async () => {
    const { calls, fetchFn } = fakeFetch(
      () => sse([step(1), step(2).slice(0, 10), step(2).slice(10), new TypeError('network reset')]),
      () => sse([step(3), awaiting(4)]),
    );
    const events = await firstValueFrom(new OpsApi(transport(fetchFn)).streamInvestigationEvents('inv-1').pipe(toArray()));
    expect(events.map((e) => e.id)).toEqual([1, 2, 3, 4]);
    expect(calls.map((c) => c.headers.get('Last-Event-ID'))).toEqual([null, '2']);
    expect(calls.every((c) => c.url === `${BASE}/investigations/inv-1/events`)).toBe(true);
    expect(calls[0].headers.get('Accept')).toBe('text/event-stream');
  });

  it('starts after the given id (a refreshed page that already has events 1..5)', async () => {
    const { calls, fetchFn } = fakeFetch(() => sse([invEvent(6, 'status_changed', { status: 'completed' })]));
    const events = await firstValueFrom(new OpsApi(transport(fetchFn)).streamInvestigationEvents('inv-1', 5).pipe(toArray()));
    expect(events.map((e) => e.type)).toEqual(['status_changed']);
    expect(calls[0].headers.get('Last-Event-ID')).toBe('5');
  });

  it('retries 503 from a proxy while the agent service restarts', async () => {
    const { calls, fetchFn } = fakeFetch(
      () => problem(503, 'Service unavailable'),
      () => sse([awaiting(1)]),
    );
    const events = await firstValueFrom(new OpsApi(transport(fetchFn)).streamInvestigationEvents('inv-1').pipe(toArray()));
    expect(events).toHaveLength(1);
    expect(calls).toHaveLength(2);
  });

  it('does not retry a 404', async () => {
    const { calls, fetchFn } = fakeFetch(() => problem(404, 'Investigation not found'));
    const err = await firstValueFrom(new OpsApi(transport(fetchFn)).streamInvestigationEvents('nope')).catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(404);
    expect(calls).toHaveLength(1);
  });

  it('completes when the server closes without a final event but the run has stopped', async () => {
    const { calls, fetchFn } = fakeFetch(
      () => sse([]), // nothing after Last-Event-ID: the final event came earlier
      () => investigation('awaiting_review'),
    );
    const events = await firstValueFrom(new OpsApi(transport(fetchFn)).streamInvestigationEvents('inv-1', 9).pipe(toArray()));
    expect(events).toEqual([]);
    expect(calls.map((c) => c.url)).toEqual([`${BASE}/investigations/inv-1/events`, `${BASE}/investigations/inv-1`]);
  });

  it('reconnects when the server closes early while the run is still going (idle proxy timeout)', async () => {
    const { calls, fetchFn } = fakeFetch(
      () => sse([step(1)]),
      () => investigation('running'),
      () => sse([awaiting(2)]),
    );
    const events = await firstValueFrom(new OpsApi(transport(fetchFn)).streamInvestigationEvents('inv-1').pipe(toArray()));
    expect(events.map((e) => e.id)).toEqual([1, 2]);
    expect(calls[2].headers.get('Last-Event-ID')).toBe('1');
  });

  it('honours the retry: field before reconnecting', async () => {
    const { calls, fetchFn } = fakeFetch(
      () => sse([`retry: 5\n${step(1)}`, new TypeError('reset')]),
      () => sse([awaiting(2)]),
    );
    const started = Date.now();
    await firstValueFrom(new OpsApi(transport(fetchFn)).streamInvestigationEvents('inv-1').pipe(toArray()));
    expect(Date.now() - started).toBeGreaterThanOrEqual(4);
    expect(calls).toHaveLength(2);
  });
});

describe('http client: live streams', () => {
  it('watch mode reopens the conversation stream after a network drop', async () => {
    const ev = (id: string) =>
      `event: conversation_status\ndata: ${JSON.stringify({ id, type: 'conversation_status', conversation_id: 'conv', created_at: '2026-10-28T10:00:00Z', status: 'open' })}\n\n`;
    const { calls, fetchFn } = fakeFetch(
      () => sse([ev('a'), new TypeError('reset')]),
      () => sse([ev('b')]),
    );
    const events = await firstValueFrom(new SharedApi(transport(fetchFn)).conversationEvents('conv').pipe(take(2), toArray()));
    expect(events.map((e) => e.id)).toEqual(['a', 'b']);
    expect(calls).toHaveLength(2);
    expect(calls[1].headers.get('Last-Event-ID')).toBeNull(); // no resume for watch mode
  });

  it('the case queue reopens after a 502 and asks for a reload; a 400 is final', async () => {
    const created = `event: status_changed\ndata: ${JSON.stringify({ id: '1', type: 'status_changed', created_at: '2026-10-28T10:00:00Z', case_id: 'c', status: 'investigating' })}\n\n`;
    const { calls, fetchFn } = fakeFetch(
      () => problem(502, 'Bad gateway'),
      () => sse([created]),
    );
    let reloads = 0;
    const events = await firstValueFrom(new OpsApi(transport(fetchFn)).caseEvents(() => reloads++).pipe(take(1), toArray()));
    expect(events).toHaveLength(1);
    expect(calls).toHaveLength(2);
    expect(reloads).toBe(1);

    const final = fakeFetch(() => problem(400, 'Invalid request'));
    const err = await firstValueFrom(new OpsApi(transport(final.fetchFn)).caseEvents()).catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(final.calls).toHaveLength(1);
  });
});
