import { firstValueFrom, toArray } from 'rxjs';
import { ApiError, FetchFn, SseMessage, readSse, resumableSse, sseRequest } from './sse';

function streamOf(chunks: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      controller.close();
    },
  });
}

async function collect(chunks: string[]): Promise<SseMessage[]> {
  const out: SseMessage[] = [];
  for await (const msg of readSse(streamOf(chunks))) out.push(msg);
  return out;
}

describe('readSse', () => {
  it('parses event, id and data fields', async () => {
    const msgs = await collect(['event: token\nid: 7\ndata: {"text":"Hi"}\n\n']);
    expect(msgs).toEqual([{ event: 'token', id: '7', data: '{"text":"Hi"}' }]);
  });

  it('handles messages split across chunks, including a split CRLF', async () => {
    const msgs = await collect(['event: a\r', '\ndata: 1\r\n', '\r\nevent: b\nda', 'ta: 2\n', '\n']);
    expect(msgs.map((m) => [m.event, m.data])).toEqual([
      ['a', '1'],
      ['b', '2'],
    ]);
  });

  it('joins multi-line data, skips comments and defaults the event name', async () => {
    const msgs = await collect([': keep-alive\n\ndata: line 1\ndata: line 2\n\n']);
    expect(msgs).toEqual([{ event: 'message', data: 'line 1\nline 2' }]);
  });

  it('reads retry and ignores blocks without data', async () => {
    const msgs = await collect(['event: ping\n\nretry: 3000\ndata: x\n\n']);
    expect(msgs).toEqual([{ event: 'message', data: 'x', retry: 3000 }]);
  });

  it('drops an unterminated last message', async () => {
    const msgs = await collect(['data: complete\n\ndata: partial']);
    expect(msgs.map((m) => m.data)).toEqual(['complete']);
  });
});

describe('sseRequest', () => {
  it('sends Accept: text/event-stream and the request body', async () => {
    let seen: RequestInit | undefined;
    const fetchFn: FetchFn = async (_url, init) => {
      seen = init;
      return new Response(streamOf(['data: 1\n\n']), { headers: { 'Content-Type': 'text/event-stream' } });
    };
    const msgs = await firstValueFrom(sseRequest(fetchFn, '/x', { method: 'POST', body: '{"a":1}' }).pipe(toArray()));
    expect(msgs.map((m) => m.data)).toEqual(['1']);
    expect(new Headers(seen!.headers).get('Accept')).toBe('text/event-stream');
    expect(seen!.method).toBe('POST');
    expect(seen!.body).toBe('{"a":1}');
  });

  it('errors with the problem details on a non-2xx response', async () => {
    const fetchFn: FetchFn = async () =>
      new Response(JSON.stringify({ title: 'Conversation is closed', status: 409 }), {
        status: 409,
        headers: { 'Content-Type': 'application/problem+json' },
      });
    const err = await firstValueFrom(sseRequest(fetchFn, '/x')).catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(409);
    expect((err as ApiError).problem.title).toBe('Conversation is closed');
  });

  it('aborts the request when unsubscribed', async () => {
    let signal: AbortSignal | undefined;
    const fetchFn: FetchFn = async (_url, init) => {
      signal = init?.signal ?? undefined;
      return new Response(new ReadableStream({ start() {} }));
    };
    const sub = sseRequest(fetchFn, '/x').subscribe();
    await new Promise((r) => setTimeout(r, 0));
    sub.unsubscribe();
    expect(signal?.aborted).toBe(true);
  });
});

describe('resumableSse', () => {
  it('reconnects with Last-Event-ID after a drop and completes after the final message', async () => {
    const lastIds: (string | null)[] = [];
    const responses = [
      // first connection drops after event 2
      ['id: 1\nevent: step\ndata: a\n\n', 'id: 2\nevent: step\ndata: b\n\n'],
      // second connection replays after 2, then the final event, then closes
      ['id: 3\nevent: step\ndata: c\n\n', 'id: 4\nevent: done\ndata: d\n\n'],
    ];
    const fetchFn: FetchFn = async (_url, init) => {
      lastIds.push(new Headers(init?.headers).get('Last-Event-ID'));
      return new Response(streamOf(responses.shift() ?? []));
    };
    const msgs = await firstValueFrom(
      resumableSse(fetchFn, '/events', { isFinal: (m) => m.event === 'done', retryDelayMs: 0 }).pipe(toArray()),
    );
    expect(msgs.map((m) => m.data)).toEqual(['a', 'b', 'c', 'd']);
    expect(lastIds).toEqual([null, '2']);
  });

  it('starts from the given lastEventId', async () => {
    let lastId: string | null = null;
    const fetchFn: FetchFn = async (_url, init) => {
      lastId = new Headers(init?.headers).get('Last-Event-ID');
      return new Response(streamOf(['id: 9\nevent: done\ndata: x\n\n']));
    };
    await firstValueFrom(resumableSse(fetchFn, '/e', { lastEventId: '8', isFinal: () => true }).pipe(toArray()));
    expect(lastId).toBe('8');
  });

  it('gives up after maxRetries connections that deliver nothing', async () => {
    let calls = 0;
    const fetchFn: FetchFn = async () => {
      calls++;
      throw new TypeError('network down');
    };
    const err = await firstValueFrom(
      resumableSse(fetchFn, '/e', { isFinal: () => false, retryDelayMs: 0, maxRetries: 2 }),
    ).catch((e) => e);
    expect(err).toBeInstanceOf(TypeError);
    expect(calls).toBe(3);
  });

  it('does not retry HTTP errors', async () => {
    let calls = 0;
    const fetchFn: FetchFn = async () => {
      calls++;
      return new Response(JSON.stringify({ title: 'Investigation not found', status: 404 }), { status: 404 });
    };
    const err = await firstValueFrom(resumableSse(fetchFn, '/e', { isFinal: () => false, retryDelayMs: 0 })).catch(
      (e) => e,
    );
    expect(err).toBeInstanceOf(ApiError);
    expect(calls).toBe(1);
  });
});
