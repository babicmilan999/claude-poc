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

const STATUS_TITLES: Record<number, string> = {
  400: 'Bad request',
  401: 'Unauthorized',
  404: 'Not found',
  409: 'Conflict',
  422: 'Invalid request',
  500: 'Internal server error',
  502: 'Bad gateway',
  503: 'Service unavailable',
  504: 'Gateway timeout',
};

/**
 * A non-2xx response, with its `application/problem+json` body when there is one. Bodies
 * that are not problem details (FastAPI's `{"detail": "Not Found"}` for an unknown route,
 * a proxy's HTML error page) still give a usable title.
 */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly problem: Problem,
  ) {
    super(problem.detail ? `${problem.title}: ${problem.detail}` : problem.title);
    this.name = 'ApiError';
  }

  static async fromResponse(res: Response): Promise<ApiError> {
    const fallback = res.statusText || STATUS_TITLES[res.status] || `HTTP ${res.status}`;
    let problem: Problem = { title: fallback, status: res.status };
    try {
      const body = (await res.json()) as Partial<Problem> & { detail?: unknown };
      if (body && typeof body.title === 'string') {
        problem = { ...problem, ...body, status: res.status } as Problem;
      } else if (body && typeof body.detail === 'string') {
        problem = { title: body.detail, status: res.status };
      }
    } catch {
      // not JSON; keep the status text
    }
    return new ApiError(res.status, problem);
  }
}

/** The agent service could not be reached, or the connection dropped mid-response. */
export class NetworkError extends Error {
  constructor(
    readonly url: string,
    override readonly cause?: unknown,
  ) {
    super(`Cannot reach the agent service (${url})`);
    this.name = 'NetworkError';
  }
}

/** Statuses a proxy returns while the agent service restarts: worth retrying a stream. */
const TRANSIENT_STATUSES = [502, 503, 504];

/** True for errors a stream should reconnect after: network errors and 502/503/504. */
export function isTransientError(err: unknown): boolean {
  if (err instanceof ApiError) return TRANSIENT_STATUSES.includes(err.status);
  return true;
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
 * Errors with `ApiError` on a non-2xx response, `NetworkError` when the request fails or
 * the connection drops.
 */
export function sseRequest(fetchFn: FetchFn, url: string, init: RequestInit = {}): Observable<SseMessage> {
  return new Observable<SseMessage>((subscriber) => {
    const controller = new AbortController();
    const headers = new Headers(init.headers);
    headers.set('Accept', 'text/event-stream');
    (async () => {
      try {
        let res: Response;
        try {
          res = await fetchFn(url, { ...init, headers, signal: controller.signal });
        } catch (err) {
          throw new NetworkError(url, err);
        }
        if (!res.ok) throw await ApiError.fromResponse(res);
        if (!res.body) throw new Error('SSE response has no body');
        try {
          for await (const msg of readSse(res.body)) {
            if (subscriber.closed) return;
            subscriber.next(msg);
          }
        } catch (err) {
          throw new NetworkError(url, err);
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
  /**
   * Asked when the stream closed normally without a final message. Resolving `true` (the
   * run has stopped anyway, e.g. the final event came before `lastEventId`) completes the
   * stream instead of reconnecting. Errors count as `false`.
   */
  hasStopped?: () => Promise<boolean>;
}

/**
 * A resumable GET stream (the investigation timeline). Tracks the last `id:` seen; when
 * the connection drops or closes before a final message, reconnects with
 * `Last-Event-ID`, so the server replays only what was missed. Completes after the
 * server closes the stream right after a final message (a final message followed by
 * non-final ones, as after a reject and re-run, does not count). HTTP errors (4xx/5xx) are not
 * retried, except 502/503/504 (a proxy in front of a restarting service).
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
      const reconnect = (err?: unknown) => {
        failures = receivedAny ? 1 : failures + 1;
        if (failures > maxRetries) {
          subscriber.error(err ?? new Error('SSE stream closed before the run stopped'));
          return;
        }
        timer = setTimeout(connect, retryDelay);
      };
      const onEnd = async (err?: unknown) => {
        if (subscriber.closed) return;
        if (sawFinal) {
          subscriber.complete();
          return;
        }
        if (err !== undefined && !isTransientError(err)) {
          subscriber.error(err);
          return;
        }
        if (err === undefined && options.hasStopped) {
          const stopped = await options.hasStopped().catch(() => false);
          if (subscriber.closed) return;
          if (stopped) {
            subscriber.complete();
            return;
          }
        }
        reconnect(err);
      };
      inner = sseRequest(fetchFn, url, { method: 'GET', headers }).subscribe({
        next: (msg) => {
          receivedAny = true;
          if (msg.id !== undefined) lastEventId = msg.id;
          if (msg.retry !== undefined) retryDelay = msg.retry;
          // Only the latest message decides: a final event replayed from an earlier
          // round (e.g. `awaiting_review` before a reject) is followed by newer
          // non-final events, and the stream must keep going after those.
          sawFinal = options.isFinal(msg);
          subscriber.next(msg);
        },
        error: (err) => void onEnd(err),
        complete: () => void onEnd(),
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
