import { Observable, defer, map, repeat, retry, timer } from 'rxjs';
import {
  Case,
  CaseDetail,
  CaseEvent,
  CasePage,
  CaseQuery,
  ChatEvent,
  Conversation,
  ConversationDetail,
  ConversationPage,
  ConversationQuery,
  CreateCaseRequest,
  CustomerOverview,
  CustomerSummary,
  FeedbackRequest,
  Investigation,
  InvestigationEvent,
  InvestigationStatus,
  Message,
  Rating,
  STOPPED_INVESTIGATION_STATUSES,
  SimulatedCustomer,
  SimulationRequest,
} from './models';
import { ApiError, FetchFn, SseMessage, parseEventData, resumableSse, sseRequest } from './sse';

export interface ApiTransportConfig {
  /** Agent service base URL, e.g. `http://localhost:8000`. Empty for same origin. */
  baseUrl: string;
  /** `fetch`, or the mock server's `fetch`. */
  fetch: FetchFn;
  /** Wait before reopening a dropped stream. Default 1000 ms. */
  reconnectDelayMs?: number;
}

type QueryValue = string | number | boolean | string[] | undefined | null;

interface RequestOptions {
  query?: Record<string, QueryValue>;
  body?: unknown;
  headers?: Record<string, string>;
}

/** HTTP plumbing shared by the three API groups. */
export class ApiTransport {
  constructor(readonly config: ApiTransportConfig) {}

  get reconnectDelayMs(): number {
    return this.config.reconnectDelayMs ?? 1000;
  }

  url(path: string, query?: Record<string, QueryValue>): string {
    const params = new URLSearchParams();
    for (const [key, value] of Object.entries(query ?? {})) {
      if (value === undefined || value === null) continue;
      if (Array.isArray(value)) {
        // style: form, explode: false -> status=new,investigating
        if (value.length) params.set(key, value.join(','));
      } else {
        params.set(key, String(value));
      }
    }
    const qs = params.toString();
    return `${this.config.baseUrl.replace(/\/$/, '')}${path}${qs ? `?${qs}` : ''}`;
  }

  async json<T>(method: string, path: string, options: RequestOptions = {}): Promise<T> {
    const headers = new Headers(options.headers);
    headers.set('Accept', 'application/json');
    let body: string | undefined;
    if (options.body !== undefined) {
      headers.set('Content-Type', 'application/json');
      body = JSON.stringify(options.body);
    }
    const res = await this.config.fetch(this.url(path, options.query), { method, headers, body });
    if (!res.ok) throw await ApiError.fromResponse(res);
    return (await res.json()) as T;
  }

  sse(method: string, path: string, options: RequestOptions = {}): Observable<SseMessage> {
    const headers = new Headers(options.headers);
    let body: string | undefined;
    if (options.body !== undefined) {
      headers.set('Content-Type', 'application/json');
      body = JSON.stringify(options.body);
    }
    return sseRequest(this.config.fetch, this.url(path, options.query), { method, headers, body });
  }
}

/** Retries a live stream (not resumable) after network errors; HTTP errors are final. */
function reopenOnDrop<T>(source: Observable<T>, delayMs: number): Observable<T> {
  return source.pipe(
    retry({
      delay: (err) => {
        if (err instanceof ApiError) throw err;
        return timer(delayMs);
      },
    }),
  );
}

const enc = encodeURIComponent;

/**
 * Customer app endpoints (tags `customer-account`, `customer-chat`). Every call sends the
 * logged-in customer as `X-Customer-Id`.
 */
export class CustomerApi {
  constructor(
    private readonly transport: ApiTransport,
    private readonly customerId: () => string | null,
  ) {}

  private headers(): Record<string, string> {
    const id = this.customerId();
    if (!id) throw new Error('No customer selected ("log in as" first)');
    return { 'X-Customer-Id': id };
  }

  async getMyOverview(): Promise<CustomerOverview> {
    return this.transport.json('GET', '/me/overview', { headers: this.headers() });
  }

  async createConversation(): Promise<Conversation> {
    return this.transport.json('POST', '/conversations', { headers: this.headers() });
  }

  /** Sends a message; the reply streams back as chat events. Unsubscribe to cancel. */
  sendMessage(conversationId: string, content: string): Observable<ChatEvent> {
    return defer(() =>
      this.transport.sse('POST', `/conversations/${enc(conversationId)}/messages`, {
        headers: this.headers(),
        body: { content },
      }),
    ).pipe(map((msg) => parseEventData<ChatEvent>(msg)));
  }

  async rateMessage(conversationId: string, messageId: number, rating: Rating): Promise<Message> {
    return this.transport.json('PUT', `/conversations/${enc(conversationId)}/messages/${messageId}/rating`, {
      headers: this.headers(),
      body: { rating },
    });
  }
}

/** Ops console endpoints (tags `ops-cases`, `ops-investigations`, `ops-simulation`). */
export class OpsApi {
  constructor(private readonly transport: ApiTransport) {}

  listCases(query: CaseQuery = {}): Promise<CasePage> {
    return this.transport.json('GET', '/cases', { query: { ...query } });
  }

  /**
   * Live case queue updates (not stored). Reopens after a drop; `onReconnect` runs then,
   * because the console must reload `GET /cases` to catch up.
   */
  caseEvents(onReconnect?: () => void): Observable<CaseEvent> {
    let opened = 0;
    const stream = new Observable<SseMessage>((subscriber) => {
      if (opened++ > 0) onReconnect?.();
      return this.transport.sse('GET', '/cases/events').subscribe(subscriber);
    });
    // The queue stream is meant to stay open: reopen after a drop or a normal close.
    return reopenOnDrop(stream, this.transport.reconnectDelayMs).pipe(
      repeat({ delay: () => timer(this.transport.reconnectDelayMs) }),
      map((msg) => parseEventData<CaseEvent>(msg)),
    );
  }

  createCase(request: CreateCaseRequest): Promise<Case> {
    return this.transport.json('POST', '/cases', { body: request });
  }

  getCase(caseId: string): Promise<CaseDetail> {
    return this.transport.json('GET', `/cases/${enc(caseId)}`);
  }

  /** Returns the new manual investigation, or the one already active. */
  investigateCase(caseId: string): Promise<Investigation> {
    return this.transport.json('POST', `/cases/${enc(caseId)}/investigate`);
  }

  reinvestigateCase(caseId: string): Promise<Investigation> {
    return this.transport.json('POST', `/cases/${enc(caseId)}/reinvestigate`);
  }

  getInvestigation(investigationId: string): Promise<Investigation> {
    return this.transport.json('GET', `/investigations/${enc(investigationId)}`);
  }

  /** The stored timeline as a list (for finished runs). */
  getInvestigationEvents(investigationId: string, afterId?: number): Promise<InvestigationEvent[]> {
    return this.transport.json('GET', `/investigations/${enc(investigationId)}/events`, {
      query: { after_id: afterId },
    });
  }

  /**
   * The timeline, live: replays stored events after `afterId` (all when omitted), then
   * streams until the run stops. Reconnects with `Last-Event-ID` after a drop.
   */
  streamInvestigationEvents(investigationId: string, afterId?: number): Observable<InvestigationEvent> {
    return resumableSse(this.transport.config.fetch, this.transport.url(`/investigations/${enc(investigationId)}/events`), {
      lastEventId: afterId === undefined ? undefined : String(afterId),
      retryDelayMs: this.transport.reconnectDelayMs,
      isFinal: isFinalInvestigationMessage,
    }).pipe(map((msg) => parseEventData<InvestigationEvent>(msg)));
  }

  submitFeedback(investigationId: string, feedback: FeedbackRequest): Promise<Investigation> {
    return this.transport.json('POST', `/investigations/${enc(investigationId)}/feedback`, { body: feedback });
  }

  simulateCustomer(request: SimulationRequest = {}): Promise<SimulatedCustomer> {
    return this.transport.json('POST', '/simulation/customers', { body: request });
  }

  simulateEmail(request: SimulationRequest = {}): Promise<Case> {
    return this.transport.json('POST', '/simulation/emails', { body: request });
  }
}

/** The run stopped: the server closes the timeline stream after this message. */
export function isFinalInvestigationMessage(msg: SseMessage): boolean {
  if (msg.event === 'awaiting_review') return true;
  if (msg.event !== 'status_changed') return false;
  const event = parseEventData<{ payload?: { status?: InvestigationStatus } }>(msg);
  return !!event.payload?.status && STOPPED_INVESTIGATION_STATUSES.includes(event.payload.status);
}

/** Endpoints both apps read (tags `shared-customers`, `shared-conversations`). */
export class SharedApi {
  constructor(private readonly transport: ApiTransport) {}

  listCustomers(): Promise<CustomerSummary[]> {
    return this.transport.json('GET', '/customers');
  }

  listConversations(query: ConversationQuery = {}): Promise<ConversationPage> {
    return this.transport.json('GET', '/conversations', { query: { ...query } });
  }

  getConversation(conversationId: string): Promise<ConversationDetail> {
    return this.transport.json('GET', `/conversations/${enc(conversationId)}`);
  }

  /**
   * Watch mode: stored messages replayed as `message_created`, then live events. No
   * resume (tokens are not stored): after a drop the stream is simply reopened, and the
   * replay is deduplicated by message id in the chat state.
   */
  conversationEvents(conversationId: string): Observable<ChatEvent> {
    return reopenOnDrop(
      this.transport.sse('GET', `/conversations/${enc(conversationId)}/events`),
      this.transport.reconnectDelayMs,
    ).pipe(map((msg) => parseEventData<ChatEvent>(msg)));
  }
}
