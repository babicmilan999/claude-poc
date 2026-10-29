// An in-memory mock of the agent service that follows contracts/agent-api.yaml, served
// through a `fetch`-compatible function. The real API client runs unchanged on top of it,
// SSE streams included, so the apps work without a backend and the tests exercise the same
// code paths as production. The contract tests validate its responses against the spec.

import {
  CASE_SOURCES,
  CASE_STATUSES,
  CONVERSATION_OUTCOMES,
  CONVERSATION_STATUSES,
  Case,
  CaseDetail,
  CaseEvent,
  CaseStatus,
  ChatEvent,
  Conversation,
  ConversationDetail,
  CustomerOverview,
  CustomerSummary,
  Diagnosis,
  Evidence,
  FeedbackRequest,
  ISSUE_TYPES,
  Investigation,
  InvestigationEvent,
  InvestigationSummary,
  IssueType,
  Message,
  NEW_CASE_SOURCES,
  Problem,
  ROOT_CAUSES,
  RootCause,
  STOPPED_INVESTIGATION_STATUSES,
  SimulatedCustomer,
  Source,
} from '../models';
import {
  FIRST_NAMES,
  GENERIC_ANSWER,
  GENERIC_SOURCES,
  HANDOFF_NOTICE,
  IncidentScript,
  LAST_NAMES,
  SCRIPTS,
  asksForHuman,
  classifyText,
  scriptFor,
} from './scenarios';

export interface MockServerOptions {
  /** Delay between streamed events and run steps, in ms. Default 120; use 0 in tests. */
  tickMs?: number;
  /** Seed customers, conversations and cases. Default true. */
  seed?: boolean;
  /** Random source (for repeatable tests). Default `Math.random`. */
  random?: () => number;
  /** Issue types investigated automatically (AUTO_INVESTIGATE_TYPES). */
  autoInvestigateTypes?: IssueType[];
}

interface MockCustomer {
  summary: CustomerSummary;
  overview: CustomerOverview;
  incident: IncidentScript | null;
}

interface ConversationRecord {
  conversation: Conversation;
  messages: Message[];
  downCount: number;
  seq: number;
  busy: boolean;
}

interface CaseRecord {
  kase: Case;
  transcript: Message[];
  handoffEvidence: Evidence[];
  incident: IncidentScript | null;
}

interface InvestigationRecord {
  investigation: Investigation;
  events: InvestigationEvent[];
  hint: string | null;
  evidenceSeq: number;
}

class HttpProblem extends Error {
  constructor(
    readonly status: number,
    readonly title: string,
    readonly detail?: string,
  ) {
    super(title);
  }
}

type Listener = (event: unknown) => void;

class Bus {
  private readonly topics = new Map<string, Set<Listener>>();

  on(topic: string, listener: Listener): () => void {
    let set = this.topics.get(topic);
    if (!set) this.topics.set(topic, (set = new Set()));
    set.add(listener);
    return () => set!.delete(listener);
  }

  emit(topic: string, event: unknown): void {
    for (const listener of [...(this.topics.get(topic) ?? [])]) listener(event);
  }
}

interface SseSink {
  send(type: string, data: unknown, id?: string | number): void;
  close(): void;
  onClose(fn: () => void): void;
}

const AUTO_DEFAULT: IssueType[] = ['card_decline', 'missed_payment', 'failed_direct_debit'];

export class MockAgentServer {
  /** Pass this as the API client's `fetch`. */
  readonly fetch = (input: string, init?: RequestInit): Promise<Response> => this.handle(input, init ?? {});

  /** Resolves once the seed data is in place. */
  readonly ready: Promise<void>;

  private readonly tickMs: number;
  private readonly random: () => number;
  private readonly autoTypes: IssueType[];
  private instant = false;
  private clockOffsetMs = 0;
  private nextMessageId = 1;
  private nextEventId = 1;
  private nextCaseEventId = 1;
  private readonly bus = new Bus();
  private readonly running = new Set<Promise<void>>();
  /** Open SSE streams, so tests can drop them. */
  private readonly openStreams = new Set<() => void>();
  /** While set, background runs wait at their next step. */
  private hold: Promise<void> | null = null;

  private readonly customers = new Map<string, MockCustomer>();
  private readonly conversations = new Map<string, ConversationRecord>();
  private readonly cases = new Map<string, CaseRecord>();
  private readonly investigations = new Map<string, InvestigationRecord>();

  constructor(options: MockServerOptions = {}) {
    this.tickMs = options.tickMs ?? 120;
    this.random = options.random ?? Math.random;
    this.autoTypes = options.autoInvestigateTypes ?? AUTO_DEFAULT;
    this.ready = options.seed === false ? Promise.resolve() : this.seed();
  }

  /**
   * Test hook: background runs (investigations, simulations) stop at their next step
   * until the returned function is called. Lets a test drop a stream mid-run.
   */
  holdRuns(): () => void {
    let release!: () => void;
    const gate = new Promise<void>((resolve) => (release = resolve));
    this.hold = gate;
    return () => {
      if (this.hold === gate) this.hold = null;
      release();
    };
  }

  /**
   * Test hook: cuts every open SSE stream as a network failure would (the reader
   * errors, no clean close). Clients are expected to reconnect.
   */
  dropStreams(): number {
    const drops = [...this.openStreams];
    drops.forEach((drop) => drop());
    return drops.length;
  }

  /** Waits until every background run (investigations, simulations) has finished. */
  async settle(): Promise<void> {
    while (this.running.size) await Promise.all([...this.running]);
  }

  // ===== plumbing ============================================================

  private now(): string {
    return new Date(Date.now() + this.clockOffsetMs).toISOString();
  }

  private uuid(): string {
    const hex = Array.from({ length: 32 }, () => Math.floor(this.random() * 16).toString(16));
    hex[12] = '4';
    hex[16] = ((parseInt(hex[16], 16) & 0x3) | 0x8).toString(16);
    const s = hex.join('');
    return `${s.slice(0, 8)}-${s.slice(8, 12)}-${s.slice(12, 16)}-${s.slice(16, 20)}-${s.slice(20)}`;
  }

  private pick<T>(items: readonly T[]): T {
    return items[Math.floor(this.random() * items.length)];
  }

  private async pause(factor = 1): Promise<void> {
    while (this.hold) await this.hold;
    if (this.instant) return;
    await new Promise((resolve) => setTimeout(resolve, this.tickMs * factor));
  }

  private background(task: () => Promise<void>): Promise<void> {
    const promise = task()
      .catch((err) => console.error('[mock agent service]', err))
      .finally(() => this.running.delete(promise));
    this.running.add(promise);
    return promise;
  }

  private json(status: number, body: unknown): Response {
    return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
  }

  private problem(status: number, title: string, detail?: string): Response {
    const body: Problem = { title, status, ...(detail ? { detail } : {}) };
    return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/problem+json' } });
  }

  private sse(signal: AbortSignal | null | undefined, start: (sink: SseSink) => void): Response {
    const encoder = new TextEncoder();
    const cleanups: (() => void)[] = [];
    let closed = false;
    let drop: (() => void) | undefined;
    const finish = () => {
      closed = true;
      if (drop) this.openStreams.delete(drop);
      cleanups.splice(0).forEach((fn) => fn());
    };
    const stream = new ReadableStream<Uint8Array>({
      start: (controller) => {
        drop = () => {
          if (closed) return;
          finish();
          controller.error(new TypeError('network error (mock drop)'));
        };
        this.openStreams.add(drop);
        const sink: SseSink = {
          send: (type, data, id) => {
            if (closed) return;
            const idLine = id === undefined ? '' : `id: ${id}\n`;
            controller.enqueue(encoder.encode(`event: ${type}\n${idLine}data: ${JSON.stringify(data)}\n\n`));
          },
          close: () => {
            if (closed) return;
            finish();
            controller.close();
          },
          onClose: (fn) => (closed ? fn() : cleanups.push(fn)),
        };
        signal?.addEventListener('abort', () => {
          if (closed) return;
          finish();
          controller.error(new DOMException('The request was aborted', 'AbortError'));
        });
        if (signal?.aborted) {
          finish();
          controller.error(new DOMException('The request was aborted', 'AbortError'));
          return;
        }
        start(sink);
      },
      cancel: () => finish(),
    });
    return new Response(stream, {
      status: 200,
      headers: { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' },
    });
  }

  // ===== routing =============================================================

  private async handle(input: string, init: RequestInit): Promise<Response> {
    await this.ready;
    const url = new URL(input, 'http://mock.local');
    const method = (init.method ?? 'GET').toUpperCase();
    const headers = new Headers(init.headers);
    let body: unknown = undefined;
    if (typeof init.body === 'string' && init.body.length) {
      try {
        body = JSON.parse(init.body);
      } catch {
        return this.problem(400, 'Invalid JSON body');
      }
    }
    try {
      return this.route(method, url, headers, body, init.signal);
    } catch (err) {
      if (err instanceof HttpProblem) return this.problem(err.status, err.title, err.detail);
      throw err;
    }
  }

  private route(
    method: string,
    url: URL,
    headers: Headers,
    body: unknown,
    signal: AbortSignal | null | undefined,
  ): Response {
    const seg = url.pathname.split('/').filter(Boolean);
    const q = url.searchParams;
    const is = (m: string, ...parts: string[]) =>
      method === m && seg.length === parts.length && parts.every((p, i) => p === '*' || p === seg[i]);

    // Customer app
    if (is('GET', 'me', 'overview')) return this.json(200, this.requireCustomer(headers).overview);
    if (is('POST', 'conversations')) return this.json(201, this.createConversation(this.requireCustomer(headers)));
    if (is('POST', 'conversations', '*', 'messages')) return this.sendMessage(seg[1], headers, body, signal);
    if (is('PUT', 'conversations', '*', 'messages', '*', 'rating'))
      return this.json(200, this.rateMessage(seg[1], seg[3], headers, body));

    // Ops console
    if (is('GET', 'cases')) return this.json(200, this.listCases(q));
    if (is('POST', 'cases')) return this.json(201, this.createCase(body));
    if (is('GET', 'cases', 'events')) return this.caseEvents(signal);
    if (is('GET', 'cases', '*')) return this.json(200, this.caseDetail(this.requireCase(seg[1])));
    if (is('POST', 'cases', '*', 'investigate')) return this.investigate(seg[1]);
    if (is('POST', 'cases', '*', 'reinvestigate')) return this.json(201, this.reinvestigate(seg[1]));
    if (is('GET', 'investigations', '*')) return this.json(200, this.requireInvestigation(seg[1]).investigation);
    if (is('GET', 'investigations', '*', 'events')) return this.investigationEvents(seg[1], q, headers, signal);
    if (is('POST', 'investigations', '*', 'feedback')) return this.json(202, this.feedback(seg[1], body));
    if (is('POST', 'simulation', 'customers')) return this.json(202, this.simulateCustomer(body));
    if (is('POST', 'simulation', 'emails')) return this.json(201, this.simulateEmail(body));

    // Shared
    if (is('GET', 'customers')) return this.json(200, [...this.customers.values()].map((c) => c.summary));
    if (is('GET', 'conversations')) return this.json(200, this.listConversations(q));
    if (is('GET', 'conversations', '*')) return this.json(200, this.conversationDetail(this.requireConversation(seg[1])));
    if (is('GET', 'conversations', '*', 'events')) return this.conversationEvents(seg[1], signal);

    if (is('GET', 'health')) return this.json(200, { status: 'ok', llm_profile: 'mock' });
    throw new HttpProblem(404, 'Not found', `${method} ${url.pathname}`);
  }

  private requireCustomer(headers: Headers): MockCustomer {
    const id = headers.get('X-Customer-Id');
    const customer = id ? this.customers.get(id) : undefined;
    if (!customer) throw new HttpProblem(401, 'Unknown customer', 'Set X-Customer-Id to a known customer');
    return customer;
  }

  private requireConversation(id: string): ConversationRecord {
    const rec = this.conversations.get(id);
    if (!rec) throw new HttpProblem(404, 'Conversation not found');
    return rec;
  }

  private requireCase(id: string): CaseRecord {
    const rec = this.cases.get(id);
    if (!rec) throw new HttpProblem(404, 'Case not found');
    return rec;
  }

  private requireInvestigation(id: string): InvestigationRecord {
    const rec = this.investigations.get(id);
    if (!rec) throw new HttpProblem(404, 'Investigation not found');
    return rec;
  }

  private paging(q: URLSearchParams): { limit: number; offset: number } {
    const limit = q.has('limit') ? Number(q.get('limit')) : 50;
    const offset = q.has('offset') ? Number(q.get('offset')) : 0;
    if (!Number.isInteger(limit) || limit < 1 || limit > 200) throw new HttpProblem(400, 'Invalid limit');
    if (!Number.isInteger(offset) || offset < 0) throw new HttpProblem(400, 'Invalid offset');
    return { limit, offset };
  }

  private enumParam<T extends string>(q: URLSearchParams, name: string, allowed: readonly T[]): T | undefined {
    const value = q.get(name);
    if (value === null) return undefined;
    if (!allowed.includes(value as T)) throw new HttpProblem(400, `Invalid ${name}`, value);
    return value as T;
  }

  // ===== customers ===========================================================

  private addCustomer(name: string, incident: IncidentScript | null, simulated: boolean): MockCustomer {
    const id = this.uuid();
    const last4 = String(1000 + Math.floor(this.random() * 9000));
    const summary: CustomerSummary = { id, name, simulated };
    const expiry = new Date(Date.now() + (incident?.cardStatus === 'expired' ? -20 : 400) * 86400000)
      .toISOString()
      .slice(0, 10);
    const balance = incident?.balance ?? (500 + Math.floor(this.random() * 3000)).toFixed(2);
    const tx = (days: number, amount: string, description: string) => ({
      id: this.uuid(),
      amount,
      currency: 'GBP',
      description,
      booked_at: new Date(Date.now() - days * 86400000).toISOString(),
    });
    const customer: MockCustomer = {
      summary,
      incident,
      overview: {
        customer: summary,
        accounts: [
          {
            id: this.uuid(),
            name: 'Current account',
            masked_account_number: `****${String(1000 + Math.floor(this.random() * 9000))}`,
            currency: 'GBP',
            balance,
            status: 'active',
          },
        ],
        cards: [{ id: this.uuid(), last4, status: incident?.cardStatus ?? 'active', expiry }],
        recent_transactions: [
          tx(0, '-23.40', 'TESCO STORES'),
          tx(1, '-4.10', 'PRET A MANGER'),
          tx(2, '2150.00', 'SALARY ACME LTD'),
          tx(3, '-39.99', 'FITLIFE GYMS'),
          tx(5, '-61.25', 'BRIGHT ENERGY'),
        ],
      },
    };
    this.customers.set(id, customer);
    return customer;
  }

  // ===== conversations =======================================================

  private createConversation(customer: MockCustomer, simulated = false): Conversation {
    const conversation: Conversation = {
      id: this.uuid(),
      customer_id: customer.summary.id,
      customer_name: customer.summary.name,
      status: 'open',
      outcome: null,
      category: null,
      rating: null,
      simulated,
      case_id: null,
      started_at: this.now(),
      ended_at: null,
    };
    this.conversations.set(conversation.id, { conversation, messages: [], downCount: 0, seq: 0, busy: false });
    return conversation;
  }

  private conversationDetail(rec: ConversationRecord): ConversationDetail {
    return { ...rec.conversation, messages: rec.messages };
  }

  private listConversations(q: URLSearchParams) {
    const status = this.enumParam(q, 'status', CONVERSATION_STATUSES);
    const outcome = this.enumParam(q, 'outcome', CONVERSATION_OUTCOMES);
    const simulated = q.get('simulated');
    if (simulated !== null && simulated !== 'true' && simulated !== 'false') throw new HttpProblem(400, 'Invalid simulated');
    const customerId = q.get('customer_id');
    const { limit, offset } = this.paging(q);
    const items = [...this.conversations.values()]
      .map((r) => r.conversation)
      .filter(
        (c) =>
          (!status || c.status === status) &&
          (!outcome || c.outcome === outcome) &&
          (simulated === null || c.simulated === (simulated === 'true')) &&
          (!customerId || c.customer_id === customerId),
      )
      .sort((a, b) => b.started_at.localeCompare(a.started_at));
    return { items: items.slice(offset, offset + limit), total: items.length };
  }

  private addMessage(rec: ConversationRecord, role: Message['role'], content: string, sources: Source[], id?: number): Message {
    const message: Message = {
      id: id ?? this.nextMessageId++,
      conversation_id: rec.conversation.id,
      role,
      content,
      sources,
      rating: null,
      created_at: this.now(),
    };
    rec.messages.push(message);
    return message;
  }

  private publishChat(rec: ConversationRecord, event: Record<string, unknown> & { type: ChatEvent['type'] }): void {
    const full = {
      id: String(++rec.seq),
      conversation_id: rec.conversation.id,
      created_at: this.now(),
      ...event,
    } as ChatEvent;
    this.bus.emit(`conversation:${rec.conversation.id}`, full);
  }

  private sendMessage(id: string, headers: Headers, body: unknown, signal: AbortSignal | null | undefined): Response {
    const customer = this.requireCustomer(headers);
    const rec = this.requireConversation(id);
    if (rec.conversation.customer_id !== customer.summary.id) throw new HttpProblem(404, 'Conversation not found');
    const content = (body as { content?: unknown } | undefined)?.content;
    if (typeof content !== 'string' || content.trim().length < 1 || content.length > 4000)
      throw new HttpProblem(400, 'Invalid message', 'content must be 1 to 4000 characters');
    if (rec.conversation.status !== 'open') throw new HttpProblem(409, 'Conversation is closed');
    if (rec.busy) throw new HttpProblem(409, 'The assistant is still answering');
    rec.busy = true;
    return this.sse(signal, (sink) => {
      const off = this.bus.on(`conversation:${id}`, (e) => {
        const event = e as ChatEvent;
        sink.send(event.type, event);
      });
      sink.onClose(off);
      this.background(async () => {
        try {
          await this.assistantTurn(rec, customer, content);
        } catch (err) {
          this.publishChat(rec, { type: 'error', message: 'The assistant failed; please try again.' });
          throw err;
        } finally {
          rec.busy = false;
          sink.close();
        }
      });
    });
  }

  /** One run of the "assistant graph": answer with sources, or hand off. */
  private async assistantTurn(rec: ConversationRecord, customer: MockCustomer, content: string): Promise<void> {
    const conv = rec.conversation;
    const customerMessage = this.addMessage(rec, 'customer', content.trim(), []);
    if (conv.outcome === 'resolved') conv.outcome = null;
    this.publishChat(rec, { type: 'message_created', message: customerMessage });
    await this.pause(2);

    const classified = classifyText(content);
    const category: IssueType = classified !== 'other' ? classified : (conv.category ?? customer.incident?.type ?? 'other');
    conv.category = category;
    const script = customer.incident && customer.incident.type === category ? customer.incident : null;

    if (asksForHuman(content) || rec.downCount >= 2 || script?.needsHuman) {
      await this.handoff(rec, customer, script, category);
      return;
    }

    const answer = script ? script.answer : GENERIC_ANSWER;
    const sources = script ? script.sources : GENERIC_SOURCES;
    const messageId = this.nextMessageId++;
    for (const piece of answer.match(/\S+\s*/g) ?? []) {
      this.publishChat(rec, { type: 'token', message_id: messageId, text: piece });
      await this.pause(0.4);
    }
    this.publishChat(rec, { type: 'sources', message_id: messageId, sources });
    const message = this.addMessage(rec, 'assistant', answer, sources, messageId);
    this.publishChat(rec, { type: 'message_completed', message });
  }

  private async handoff(
    rec: ConversationRecord,
    customer: MockCustomer,
    script: IncidentScript | null,
    category: IssueType,
  ): Promise<void> {
    const conv = rec.conversation;
    const notice = this.addMessage(
      rec,
      'assistant',
      `I've passed this to our support team with what I found so far. ${HANDOFF_NOTICE}`,
      [],
    );
    this.publishChat(rec, { type: 'message_created', message: notice });
    const firstCustomerMessage = rec.messages.find((m) => m.role === 'customer')?.content ?? '';
    const caseRec = this.addCase({
      customer,
      source: 'assistant_handoff',
      category,
      complaint: null,
      summary: script?.handoffSummary ?? `Customer asked for help: "${firstCustomerMessage}"`,
      conversationId: conv.id,
      simulated: conv.simulated,
      incident: customer.incident,
    });
    caseRec.transcript = rec.messages.map((m) => ({ ...m }));
    if (script) {
      const t = script.evidence[0];
      caseRec.handoffEvidence = [
        { id: 'ev-a1', origin: 'assistant', tool: t.tool, args: t.args, summary: t.summary, data: t.data, created_at: this.now() },
      ];
    }
    conv.status = 'closed';
    conv.outcome = 'handed_off';
    conv.case_id = caseRec.kase.id;
    conv.ended_at = this.now();
    this.publishChat(rec, { type: 'handoff', case_id: caseRec.kase.id, notice: HANDOFF_NOTICE });
    this.publishChat(rec, { type: 'conversation_status', status: 'closed', outcome: 'handed_off' });
    this.startInvestigation(caseRec, 'auto');
  }

  private rateMessage(conversationId: string, messageIdRaw: string, headers: Headers, body: unknown): Message {
    const customer = this.requireCustomer(headers);
    const rec = this.requireConversation(conversationId);
    if (rec.conversation.customer_id !== customer.summary.id) throw new HttpProblem(404, 'Conversation not found');
    const message = rec.messages.find((m) => String(m.id) === messageIdRaw);
    if (!message) throw new HttpProblem(404, 'Message not found');
    const rating = (body as { rating?: unknown } | undefined)?.rating;
    if (rating !== 'up' && rating !== 'down') throw new HttpProblem(400, 'rating must be up or down');
    if (message.role !== 'assistant') throw new HttpProblem(409, 'Only assistant messages can be rated');
    message.rating = rating;
    rec.conversation.rating = rating;
    if (rating === 'up') {
      rec.conversation.outcome = 'resolved';
    } else {
      rec.downCount++;
      if (rec.conversation.outcome === 'resolved') rec.conversation.outcome = null;
    }
    return message;
  }

  private conversationEvents(id: string, signal: AbortSignal | null | undefined): Response {
    const rec = this.requireConversation(id);
    return this.sse(signal, (sink) => {
      let n = 0;
      const replay = (type: string, data: Record<string, unknown>) =>
        sink.send(type, { id: `replay-${++n}`, type, conversation_id: id, created_at: this.now(), ...data });
      for (const message of rec.messages) replay('message_created', { message });
      replay('conversation_status', { status: rec.conversation.status, outcome: rec.conversation.outcome ?? null });
      if (rec.conversation.status === 'closed') {
        sink.close();
        return;
      }
      const off = this.bus.on(`conversation:${id}`, (e) => {
        const event = e as ChatEvent;
        sink.send(event.type, event);
        if (event.type === 'conversation_status' && event.status === 'closed') sink.close();
      });
      sink.onClose(off);
    });
  }

  // ===== cases ===============================================================

  private addCase(input: {
    customer: MockCustomer;
    source: Case['source'];
    category: IssueType | null;
    complaint: string | null;
    summary: string | null;
    conversationId: string | null;
    simulated: boolean;
    incident: IncidentScript | null;
    triggerPayload?: Record<string, unknown> | null;
  }): CaseRecord {
    const now = this.now();
    const kase: Case = {
      id: this.uuid(),
      customer_id: input.customer.summary.id,
      customer_name: input.customer.summary.name,
      conversation_id: input.conversationId,
      source: input.source,
      category: input.category,
      complaint: input.complaint,
      trigger_payload: input.triggerPayload ?? null,
      summary: input.summary,
      status: 'new',
      simulated: input.simulated,
      active_investigation_id: null,
      created_at: now,
      updated_at: now,
    };
    const rec: CaseRecord = { kase, transcript: [], handoffEvidence: [], incident: input.incident };
    this.cases.set(kase.id, rec);
    this.emitCase({ type: 'case_created', case: kase });
    return rec;
  }

  private emitCase(event: Record<string, unknown> & { type: CaseEvent['type'] }): void {
    this.bus.emit('cases', { id: String(this.nextCaseEventId++), created_at: this.now(), ...event });
  }

  /** Live only: no replay (contract). */
  private caseEvents(signal: AbortSignal | null | undefined): Response {
    return this.sse(signal, (sink) => {
      const off = this.bus.on('cases', (e) => {
        const event = e as CaseEvent;
        sink.send(event.type, event);
      });
      sink.onClose(off);
    });
  }

  private setCaseStatus(rec: CaseRecord, status: CaseStatus): void {
    const previous = rec.kase.status;
    rec.kase.status = status;
    rec.kase.updated_at = this.now();
    if (previous !== status) {
      this.emitCase({
        type: 'status_changed',
        case_id: rec.kase.id,
        previous_status: previous,
        status,
        category: rec.kase.category ?? null,
      });
    }
  }

  private caseDetail(rec: CaseRecord): CaseDetail {
    const investigations = [...this.investigations.values()]
      .filter((r) => r.investigation.case_id === rec.kase.id)
      .map((r) => this.summary(r.investigation))
      .sort((a, b) => b.queued_at.localeCompare(a.queued_at));
    return { ...rec.kase, transcript: rec.transcript, handoff_evidence: rec.handoffEvidence, investigations };
  }

  private summary(inv: Investigation): InvestigationSummary {
    const { id, case_id, trigger, status, issue_type, verdict, queued_at, started_at, finished_at } = inv;
    return { id, case_id, trigger, status, issue_type, verdict, queued_at, started_at, finished_at };
  }

  private listCases(q: URLSearchParams) {
    const statusParam = q.get('status');
    const statuses = statusParam
      ? statusParam.split(',').map((s) => {
          if (!CASE_STATUSES.includes(s as CaseStatus)) throw new HttpProblem(400, 'Invalid status', s);
          return s as CaseStatus;
        })
      : CASE_STATUSES.filter((s) => s !== 'resolved');
    const source = this.enumParam(q, 'source', CASE_SOURCES);
    const category = this.enumParam(q, 'category', ISSUE_TYPES);
    const { limit, offset } = this.paging(q);
    const items = [...this.cases.values()]
      .map((r) => r.kase)
      .filter((c) => statuses.includes(c.status) && (!source || c.source === source) && (!category || c.category === category))
      .sort((a, b) => a.created_at.localeCompare(b.created_at));
    return { items: items.slice(offset, offset + limit), total: items.length };
  }

  private createCase(body: unknown): Case {
    const req = (body ?? {}) as Record<string, unknown>;
    if (typeof req['customer_id'] !== 'string') throw new HttpProblem(400, 'customer_id is required');
    if (!NEW_CASE_SOURCES.includes(req['source'] as never)) throw new HttpProblem(400, 'Invalid source');
    const complaint = req['complaint'];
    const payload = req['trigger_payload'];
    const hasComplaint = typeof complaint === 'string' && complaint.trim().length > 0 && complaint.length <= 8000;
    const hasPayload = !!payload && typeof payload === 'object' && !Array.isArray(payload);
    if (complaint !== undefined && !hasComplaint) throw new HttpProblem(400, 'complaint must be 1 to 8000 characters');
    if (!hasComplaint && !hasPayload) throw new HttpProblem(400, 'complaint or trigger_payload is required');
    const customer = this.customers.get(req['customer_id'] as string);
    if (!customer) throw new HttpProblem(404, 'Customer not found');
    const rec = this.addCase({
      customer,
      source: req['source'] as Case['source'],
      category: null,
      complaint: hasComplaint ? (complaint as string) : null,
      summary: null,
      conversationId: null,
      simulated: false,
      incident: customer.incident,
      triggerPayload: hasPayload ? (payload as Record<string, unknown>) : null,
    });
    this.startInvestigation(rec, 'auto');
    return rec.kase;
  }

  private activeInvestigation(caseRec: CaseRecord): InvestigationRecord | undefined {
    const id = caseRec.kase.active_investigation_id;
    return id ? this.investigations.get(id) : undefined;
  }

  private investigate(caseId: string): Response {
    const rec = this.requireCase(caseId);
    if (rec.kase.status === 'resolved') throw new HttpProblem(409, 'The case is resolved');
    const active = this.activeInvestigation(rec);
    if (active) return this.json(200, active.investigation);
    return this.json(201, this.startInvestigation(rec, 'manual').investigation);
  }

  private reinvestigate(caseId: string): Investigation {
    const rec = this.requireCase(caseId);
    if (rec.kase.status === 'resolved') throw new HttpProblem(409, 'The case is resolved');
    const active = this.activeInvestigation(rec);
    if (active && (active.investigation.status === 'queued' || active.investigation.status === 'running'))
      throw new HttpProblem(409, 'An investigation is already queued or running');
    if (active) {
      active.investigation.status = 'superseded';
      active.investigation.finished_at = this.now();
      this.addEvent(active, 'status_changed', { status: 'superseded', error: null });
    }
    return this.startInvestigation(rec, 'manual').investigation;
  }

  // ===== investigations ======================================================

  private startInvestigation(caseRec: CaseRecord, trigger: Investigation['trigger']): InvestigationRecord {
    const investigation: Investigation = {
      id: this.uuid(),
      case_id: caseRec.kase.id,
      trigger,
      status: 'queued',
      issue_type: null,
      verdict: null,
      queued_at: this.now(),
      started_at: null,
      finished_at: null,
      attempts: 0,
      steps_used: 0,
      category_changed: false,
      evidence: [],
      diagnosis: null,
      escalation: null,
      review: null,
      error: null,
    };
    const rec: InvestigationRecord = { investigation, events: [], hint: null, evidenceSeq: 0 };
    this.investigations.set(investigation.id, rec);
    caseRec.kase.active_investigation_id = investigation.id;
    caseRec.kase.updated_at = this.now();
    this.background(() => this.runInvestigation(rec, caseRec));
    return rec;
  }

  private addEvent(rec: InvestigationRecord, type: InvestigationEvent['type'], payload: object): void {
    const event = {
      id: this.nextEventId++,
      investigation_id: rec.investigation.id,
      type,
      created_at: this.now(),
      payload,
    } as InvestigationEvent;
    rec.events.push(event);
    this.bus.emit(`investigation:${rec.investigation.id}`, event);
  }

  private async step(rec: InvestigationRecord, node: string): Promise<void> {
    this.addEvent(rec, 'step_started', { node, step: rec.investigation.steps_used });
    await this.pause();
  }

  private finishRun(rec: InvestigationRecord, caseRec: CaseRecord, status: 'completed' | 'failed', caseStatus: CaseStatus) {
    rec.investigation.status = status;
    rec.investigation.finished_at = this.now();
    if (caseRec.kase.active_investigation_id === rec.investigation.id) caseRec.kase.active_investigation_id = null;
    this.setCaseStatus(caseRec, caseStatus);
    this.addEvent(rec, 'status_changed', { status, error: rec.investigation.error ?? null });
  }

  private async runInvestigation(rec: InvestigationRecord, caseRec: CaseRecord): Promise<void> {
    const inv = rec.investigation;
    await this.pause(2);
    if (inv.status !== 'queued') return;
    inv.status = 'running';
    inv.started_at = this.now();
    inv.attempts = (inv.attempts ?? 0) + 1;
    this.addEvent(rec, 'status_changed', { status: 'running', error: null });
    this.setCaseStatus(caseRec, 'investigating');

    await this.step(rec, 'intake');
    const incident = caseRec.incident;
    let issueType: IssueType;
    if (caseRec.kase.category) {
      await this.step(rec, 'verify');
      const previous = caseRec.kase.category;
      issueType = incident && incident.type !== previous ? incident.type : previous;
      inv.category_changed = issueType !== previous;
      this.addEvent(rec, 'classified', {
        issue_type: issueType,
        confidence: inv.category_changed ? 0.72 : 0.93,
        method: 'verified',
        previous_category: previous,
        reasoning: inv.category_changed
          ? `Signals point to ${issueType}, not ${previous}.`
          : 'The handoff category matches the account signals.',
      });
    } else {
      await this.step(rec, 'triage');
      const fromText = classifyText(`${caseRec.kase.complaint ?? ''} ${JSON.stringify(caseRec.kase.trigger_payload ?? '')}`);
      issueType = fromText !== 'other' ? fromText : (incident?.type ?? 'other');
      this.addEvent(rec, 'classified', {
        issue_type: issueType,
        confidence: issueType === 'other' ? 0.55 : 0.86,
        method: 'triaged',
        previous_category: null,
        reasoning: 'Classified from the complaint and the account signals.',
      });
    }
    inv.issue_type = issueType;
    caseRec.kase.category = issueType;
    await this.step(rec, 'route');

    if (inv.trigger === 'auto' && !this.autoTypes.includes(issueType)) {
      await this.step(rec, 'park');
      this.finishRun(rec, caseRec, 'completed', 'needs_manual');
      return;
    }
    const script = incident && incident.type === issueType ? incident : null;
    await this.investigateAndPause(rec, caseRec, issueType, script, script?.rootCause ?? null);
  }

  private async toolCall(rec: InvestigationRecord, tool: string, args: Record<string, unknown>, summary: string, data: unknown) {
    const callId = `call-${rec.investigation.id.slice(0, 4)}-${rec.evidenceSeq + 1}`;
    this.addEvent(rec, 'tool_call', { call_id: callId, tool, args });
    await this.pause(2);
    const evidence: Evidence = {
      id: `ev-${++rec.evidenceSeq}`,
      origin: 'investigation',
      tool,
      args,
      summary,
      data,
      created_at: this.now(),
    };
    rec.investigation.evidence.push(evidence);
    rec.investigation.steps_used++;
    this.addEvent(rec, 'evidence', { call_id: callId, evidence });
    return evidence;
  }

  /** Investigator, evidence check, then diagnose or escalate, then pause for review. */
  private async investigateAndPause(
    rec: InvestigationRecord,
    caseRec: CaseRecord,
    issueType: IssueType,
    script: IncidentScript | null,
    rootCause: RootCause | null,
  ): Promise<void> {
    const inv = rec.investigation;
    await this.step(rec, issueType === 'other' ? 'general_investigator' : `${issueType}_investigator`);
    const found: Evidence[] = [];
    if (rec.hint) {
      found.push(
        await this.toolCall(rec, 'get_transactions', { days: 30 }, `Re-checked recent activity following the hint: "${rec.hint}"`, {
          hint_applied: true,
        }),
      );
    }
    if (script && !rec.hint) {
      for (const t of script.evidence) found.push(await this.toolCall(rec, t.tool, t.args, t.summary, t.data));
    } else if (!script) {
      found.push(
        await this.toolCall(rec, 'get_customer_profile', {}, 'Customer active; no declines, failed payments or failed collections in 30 days', {
          findings: 0,
        }),
      );
    }
    await this.step(rec, 'evidence_check');

    if (!rootCause) {
      await this.step(rec, 'escalate');
      inv.escalation = {
        summary: 'No conclusion within the step budget: the account shows nothing that explains the complaint.',
        evidence_ids: found.map((e) => e.id),
      };
      inv.diagnosis = null;
      this.addEvent(rec, 'escalated', { escalation: inv.escalation });
      inv.status = 'awaiting_review';
      this.setCaseStatus(caseRec, 'escalated');
      this.addEvent(rec, 'awaiting_review', { has_diagnosis: false });
      return;
    }

    await this.step(rec, 'diagnose');
    const base = scriptFor(rootCause);
    const explanation = base?.explanation ?? `The evidence points to ${rootCause.replace(/_/g, ' ')}.`;
    for (const piece of explanation.match(/\S+\s*/g) ?? []) {
      this.addEvent(rec, 'token', { text: piece });
      await this.pause(0.3);
    }
    const diagnosis: Diagnosis = {
      id: this.uuid(),
      issue_type: issueType,
      root_cause: rootCause,
      explanation,
      confidence: rec.hint ? 0.74 : 0.91,
      evidence_ids: inv.evidence.map((e) => e.id),
      recommended_action: base?.recommendedAction ?? 'Review the evidence and contact the customer.',
    };
    inv.diagnosis = diagnosis;
    inv.escalation = null;
    this.addEvent(rec, 'diagnosis', { diagnosis });
    await this.step(rec, 'review');
    inv.status = 'awaiting_review';
    this.setCaseStatus(caseRec, 'awaiting_review');
    this.addEvent(rec, 'awaiting_review', { has_diagnosis: true });
  }

  private investigationEvents(id: string, q: URLSearchParams, headers: Headers, signal: AbortSignal | null | undefined): Response {
    const rec = this.requireInvestigation(id);
    const raw = headers.get('Last-Event-ID') ?? q.get('after_id');
    const after = raw === null || raw === '' ? 0 : Number(raw);
    if (!Number.isFinite(after)) throw new HttpProblem(400, 'Invalid Last-Event-ID or after_id');
    const accept = headers.get('Accept') ?? '';
    if (!accept.includes('text/event-stream')) return this.json(200, rec.events.filter((e) => e.id > after));

    return this.sse(signal, (sink) => {
      for (const event of rec.events) if (event.id > after) sink.send(event.type, event, event.id);
      if (STOPPED_INVESTIGATION_STATUSES.includes(rec.investigation.status)) {
        sink.close();
        return;
      }
      const off = this.bus.on(`investigation:${id}`, (e) => {
        const event = e as InvestigationEvent;
        sink.send(event.type, event, event.id);
        const stopped =
          event.type === 'awaiting_review' ||
          (event.type === 'status_changed' && STOPPED_INVESTIGATION_STATUSES.includes(event.payload.status));
        if (stopped) sink.close();
      });
      sink.onClose(off);
    });
  }

  private feedback(id: string, body: unknown): Investigation {
    const rec = this.requireInvestigation(id);
    const req = (body ?? {}) as Partial<FeedbackRequest>;
    if (!['accepted', 'corrected', 'rejected'].includes(req.verdict as string)) throw new HttpProblem(400, 'Invalid verdict');
    if (req.corrected_root_cause !== undefined && !ROOT_CAUSES.includes(req.corrected_root_cause))
      throw new HttpProblem(400, 'Invalid corrected_root_cause');
    if (req.corrected_issue_type !== undefined && !ISSUE_TYPES.includes(req.corrected_issue_type))
      throw new HttpProblem(400, 'Invalid corrected_issue_type');
    if (req.verdict === 'corrected' && !req.corrected_root_cause)
      throw new HttpProblem(400, 'corrected requires corrected_root_cause');
    if (req.verdict === 'rejected' && !(typeof req.hint === 'string' && req.hint.trim()))
      throw new HttpProblem(400, 'rejected requires a hint');
    const inv = rec.investigation;
    if (inv.status !== 'awaiting_review') throw new HttpProblem(409, 'The investigation is not awaiting review');
    const caseRec = this.requireCase(inv.case_id);

    inv.review = {
      verdict: req.verdict!,
      corrected_issue_type: req.corrected_issue_type ?? null,
      corrected_root_cause: req.corrected_root_cause ?? null,
      hint: req.hint ?? null,
      notes: req.notes ?? null,
      created_at: this.now(),
    };
    inv.verdict = req.verdict!;
    this.addEvent(rec, 'review_recorded', { review: inv.review });

    if (req.verdict === 'rejected') {
      rec.hint = req.hint!.trim();
      inv.status = 'running';
      this.addEvent(rec, 'status_changed', { status: 'running', error: null });
      this.setCaseStatus(caseRec, 'investigating');
      const previous = inv.diagnosis?.root_cause ?? null;
      const script = caseRec.incident && caseRec.incident.type === inv.issue_type ? caseRec.incident : null;
      // The resumed run reads the hint and lands elsewhere: the alternative, or back.
      const next = script ? (previous === script.rootCause ? script.alternative : script.rootCause) : null;
      inv.diagnosis = null;
      inv.escalation = null;
      this.background(async () => {
        await this.pause(2);
        if (inv.status !== 'running') return;
        await this.investigateAndPause(rec, caseRec, inv.issue_type ?? 'other', script, next);
      });
    } else {
      this.addEvent(rec, 'step_started', { node: 'finalize', step: inv.steps_used });
      this.finishRun(rec, caseRec, 'completed', 'resolved');
    }
    return inv;
  }

  // ===== simulation ==========================================================

  private simulationScript(body: unknown): IncidentScript {
    const type = (body as { type?: unknown } | undefined)?.type;
    if (type !== undefined && !ISSUE_TYPES.includes(type as IssueType)) throw new HttpProblem(400, 'Invalid type');
    if (type === 'other') throw new HttpProblem(400, 'Type other cannot be simulated');
    const candidates = type ? SCRIPTS.filter((s) => s.type === type) : SCRIPTS;
    return this.pick(candidates);
  }

  private simulatedName(): string {
    return `${this.pick(FIRST_NAMES)} ${this.pick(LAST_NAMES)}`;
  }

  private simulateCustomer(body: unknown): SimulatedCustomer {
    const script = this.simulationScript(body);
    const customer = this.addCustomer(this.simulatedName(), script, true);
    const conversation = this.createConversation(customer, true);
    const rec = this.conversations.get(conversation.id)!;
    this.background(async () => {
      await this.pause(5);
      rec.busy = true;
      await this.assistantTurn(rec, customer, script.opening);
      rec.busy = false;
      if (conversation.status === 'closed') return;
      await this.pause(5);
      // The simulated customer is satisfied: 👍, then leaves.
      const answer = [...rec.messages].reverse().find((m) => m.role === 'assistant');
      if (answer) {
        answer.rating = 'up';
        conversation.rating = 'up';
      }
      conversation.outcome = 'resolved';
      conversation.status = 'closed';
      conversation.ended_at = this.now();
      this.publishChat(rec, { type: 'conversation_status', status: 'closed', outcome: 'resolved' });
    });
    return {
      simulation_id: this.uuid(),
      conversation_id: conversation.id,
      customer_id: customer.summary.id,
      customer_name: customer.summary.name,
    };
  }

  private simulateEmail(body: unknown): Case {
    const script = this.simulationScript(body);
    const customer = this.addCustomer(this.simulatedName(), script, true);
    const rec = this.addCase({
      customer,
      source: 'simulated_email',
      category: null,
      complaint: script.complaint,
      summary: null,
      conversationId: null,
      simulated: true,
      incident: script,
    });
    this.startInvestigation(rec, 'auto');
    return rec.kase;
  }

  // ===== seed data ===========================================================

  private async seed(): Promise<void> {
    this.instant = true;
    try {
      const s = (rc: RootCause) => scriptFor(rc)!;
      const alice = this.addCustomer('Alice Morgan', s('card_frozen_by_customer'), false);
      const ben = this.addCustomer('Ben Okafor', s('insufficient_funds'), false);
      this.addCustomer('Chloe Davies', s('mandate_cancelled_by_customer'), false);
      const daniel = this.addCustomer('Daniel Novak', s('fraud_rule_triggered'), false);
      const emma = this.addCustomer('Emma Lewis', null, false);
      this.addCustomer('Farid Haddad', s('payee_account_closed'), false);

      // Alice: resolved by the assistant, confirmed with 👍.
      this.clockOffsetMs = -26 * 3600_000;
      const aliceConv = this.conversations.get(this.createConversation(alice).id)!;
      await this.assistantTurn(aliceConv, alice, 'Why was my card declined at Tesco?');
      const answer = aliceConv.messages.find((m) => m.role === 'assistant')!;
      this.rateMessage(aliceConv.conversation.id, String(answer.id), new Headers({ 'X-Customer-Id': alice.summary.id }), {
        rating: 'up',
      });

      // Daniel: fraud block, handed off; the case waits for review.
      this.clockOffsetMs = -5 * 3600_000;
      const danielConv = this.conversations.get(this.createConversation(daniel).id)!;
      await this.assistantTurn(danielConv, daniel, daniel.incident!.opening);

      // Ben: e-mail about a missed payment; diagnosis waits for review.
      this.clockOffsetMs = -3 * 3600_000;
      this.createCase({ customer_id: ben.summary.id, source: 'email', complaint: ben.incident!.complaint });

      // Emma: a phone call about something else; parked for a manual investigation.
      this.clockOffsetMs = -1 * 3600_000;
      this.createCase({
        customer_id: emma.summary.id,
        source: 'phone',
        complaint: 'Asked why her statement shows a fee she does not recognise.',
      });
      await this.settle();
    } finally {
      this.instant = false;
      this.clockOffsetMs = 0;
    }
  }
}
