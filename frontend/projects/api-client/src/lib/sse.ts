import { Observable } from 'rxjs';
import { Problem } from './models';

/** A `fetch`-compatible function. The real one, or the mock server's. */
export type FetchFn = (input: string, init?: RequestInit) => Promise<Response>;

/** One parsed Server-Sent Events message. */
export interface SseMessage {
  /** The `event:` field; `message` when absent. */
  event: string;
  /** The `data:` lines joined with `\n`. */
  data: string;
  /** The `id:` field of this message, if it had one. */
  id?: string;
  /** The `retry:` field of this message, if it had one. */
  retry?: number;
}

/** A non-2xx response, with its `application/problem+json` body when there is one. */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly problem: Problem,
  ) {
    super(problem.detail ? `${problem.title}: ${problem.detail}` : problem.title);
    this.name = 'ApiError';
  }

  static async fromResponse(res: Response): Promise<ApiError> {
    let problem: Problem = { title: res.statusText || `HTTP ${res.status}`, status: res.status };
    try {
      const body = (await res.json()) as Partial<Problem>;
      if (body && typeof body.title === 'string') {
        problem = { ...problem, ...body } as Problem;
      }
    } catch {
      // not JSON; keep the status text
    }
    return new ApiError(res.status, problem);
  }
}

/**
 * Parses an SSE byte stream (WHATWG "event stream interpretation"): `\n`, `\r\n` or `\r`
 * line endings, `:` comments, multi-line `data:`, and a blank line ending each message.
 */
export async function* readSse(body: ReadableStream<Uint8Array>): AsyncGenerator<SseMessage> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let data: string[] = [];
  let event = '';
  let id: string | undefined;
  let retry: number | undefined;
  let hasData = false;

  function* processLine(line: string): Generator<SseMessage> {
    if (line === '') {
      if (hasData) {
        const msg: SseMessage = { event: event || 'message', data: data.join('\n') };
        if (id !== undefined) msg.id = id;
        if (retry !== undefined) msg.retry = retry;
        yield msg;
      }
      data = [];
      event = '';
      id = undefined;
      retry = undefined;
      hasData = false;
      return;
    }
    if (line.startsWith(':')) return;
    const colon = line.indexOf(':');
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? '' : line.slice(colon + 1);
    if (value.startsWith(' ')) value = value.slice(1);
    switch (field) {
      case 'data':
        data.push(value);
        hasData = true;
        break;
      case 'event':
        event = value;
        break;
      case 'id':
        if (!value.includes('\0')) id = value;
        break;
      case 'retry':
        if (/^\d+$/.test(value)) retry = Number(value);
        break;
    }
  }

  try {
    let first = true;
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      if (first) {
        if (buffer.charCodeAt(0) === 0xfeff) buffer = buffer.slice(1);
        first = false;
      }
      // A trailing '\r' may be the first half of '\r\n'; keep it until more arrives.
      let match: RegExpExecArray | null;
      const lineEnd = /\r\n|\n|\r(?!$)/g;
      let start = 0;
      while ((match = lineEnd.exec(buffer)) !== null) {
        yield* processLine(buffer.slice(start, match.index));
        start = match.index + match[0].length;
      }
      buffer = buffer.slice(start);
    }
    buffer += decoder.decode();
    // An unterminated last message is dropped, as the spec requires.
  } finally {
    reader.cancel().catch(() => undefined);
  }
}

/**
 * Opens one SSE request with `fetch` (so it can be a POST with a body, and can send
 * headers such as `X-Customer-Id` or `Last-Event-ID`). Unsubscribing aborts the request.
 * Errors with `ApiError` on a non-2xx response.
 */
export function sseRequest(fetchFn: FetchFn, url: string, init: RequestInit = {}): Observable<SseMessage> {
  return new Observable<SseMessage>((subscriber) => {
    const controller = new AbortController();
    const headers = new Headers(init.headers);
    headers.set('Accept', 'text/event-stream');
    (async () => {
      try {
        const res = await fetchFn(url, { ...init, headers, signal: controller.signal });
        if (!res.ok) throw await ApiError.fromResponse(res);
        if (!res.body) throw new Error('SSE response has no body');
        for await (const msg of readSse(res.body)) {
          if (subscriber.closed) return;
          subscriber.next(msg);
        }
        subscriber.complete();
      } catch (err) {
        // Unsubscribing aborts the request; that is not an error for anyone.
        if (!controller.signal.aborted) subscriber.error(err);
      }
    })();
    return () => controller.abort();
  });
}

export interface ResumableSseOptions {
  /** Resume after this id (sent as `Last-Event-ID`). */
  lastEventId?: string;
  /** True for the message after which the server closes the stream for good. */
  isFinal: (msg: SseMessage) => boolean;
  /** Wait before reconnecting, unless the server sent `retry:`. Default 1000 ms. */
  retryDelayMs?: number;
  /** Reconnects in a row without receiving anything. Default 5. */
  maxRetries?: number;
  headers?: HeadersInit;
}

/**
 * A resumable GET stream (the investigation timeline). Tracks the last `id:` seen; when
 * the connection drops or closes before a final message, reconnects with
 * `Last-Event-ID`, so the server replays only what was missed. Completes after the
 * server closes the stream following a final message. HTTP errors (4xx/5xx) are not retried.
 */
export function resumableSse(fetchFn: FetchFn, url: string, options: ResumableSseOptions): Observable<SseMessage> {
  return new Observable<SseMessage>((subscriber) => {
    let lastEventId = options.lastEventId;
    let retryDelay = options.retryDelayMs ?? 1000;
    const maxRetries = options.maxRetries ?? 5;
    let failures = 0;
    let sawFinal = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let inner: { unsubscribe(): void } | undefined;

    const connect = () => {
      const headers = new Headers(options.headers);
      if (lastEventId !== undefined) headers.set('Last-Event-ID', lastEventId);
      let receivedAny = false;
      const reconnectOrFail = (err?: unknown) => {
        if (subscriber.closed) return;
        if (sawFinal) {
          subscriber.complete();
          return;
        }
        if (err instanceof ApiError) {
          subscriber.error(err);
          return;
        }
        failures = receivedAny ? 1 : failures + 1;
        if (failures > maxRetries) {
          subscriber.error(err ?? new Error('SSE stream closed before the run stopped'));
          return;
        }
        timer = setTimeout(connect, retryDelay);
      };
      inner = sseRequest(fetchFn, url, { method: 'GET', headers }).subscribe({
        next: (msg) => {
          receivedAny = true;
          if (msg.id !== undefined) lastEventId = msg.id;
          if (msg.retry !== undefined) retryDelay = msg.retry;
          if (options.isFinal(msg)) sawFinal = true;
          subscriber.next(msg);
        },
        error: (err) => reconnectOrFail(err),
        complete: () => reconnectOrFail(),
      });
    };
    connect();
    return () => {
      if (timer) clearTimeout(timer);
      inner?.unsubscribe();
    };
  });
}

/** Parses the JSON `data:` of a message whose payload is a typed event object. */
export function parseEventData<T>(msg: SseMessage): T {
  return JSON.parse(msg.data) as T;
}
