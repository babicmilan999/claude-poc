import { EMPTY_CHAT, applyChatEvent } from './chat-state';
import { ChatEvent, InvestigationEvent, Message } from './models';
import { EMPTY_TIMELINE, applyInvestigationEvent, timelineFromEvents } from './timeline-state';

const C = 'c0ffee00-0000-4000-8000-000000000001';
const at = '2026-10-28T10:00:00Z';
const msg = (id: number, role: Message['role'], content: string): Message => ({
  id,
  conversation_id: C,
  role,
  content,
  sources: [],
  rating: null,
  created_at: at,
});
const chat = (e: Record<string, unknown>) => ({ id: '1', conversation_id: C, created_at: at, ...e }) as ChatEvent;

describe('applyChatEvent', () => {
  it('builds a streamed answer from tokens, then sources, then the completed message', () => {
    const events = [
      chat({ type: 'message_created', message: msg(1, 'customer', 'Why?') }),
      chat({ type: 'token', message_id: 2, text: 'Your card ' }),
      chat({ type: 'token', message_id: 2, text: 'is frozen.' }),
    ];
    let state = events.reduce(applyChatEvent, EMPTY_CHAT);
    expect(state.messages.map((m) => [m.id, m.content, m.streaming])).toEqual([
      [1, 'Why?', undefined],
      [2, 'Your card is frozen.', true],
    ]);
    state = applyChatEvent(state, chat({ type: 'sources', message_id: 2, sources: [{ kind: 'article', title: 'A' }] }));
    expect(state.messages[1].sources[0].title).toBe('A');
    state = applyChatEvent(
      state,
      chat({ type: 'message_completed', message: { ...msg(2, 'assistant', 'Your card is frozen.'), sources: [{ kind: 'article', title: 'A' }] } }),
    );
    expect(state.messages[1].streaming).toBe(false);
    expect(state.messages).toHaveLength(2);
  });

  it('deduplicates replayed messages by id and keeps them ordered', () => {
    let state = applyChatEvent(EMPTY_CHAT, chat({ type: 'message_created', message: msg(3, 'assistant', 'b') }));
    state = applyChatEvent(state, chat({ type: 'message_created', message: msg(1, 'customer', 'a') }));
    state = applyChatEvent(state, chat({ type: 'message_created', message: msg(3, 'assistant', 'b') }));
    expect(state.messages.map((m) => m.id)).toEqual([1, 3]);
  });

  it('records a handoff and the closed status', () => {
    let state = applyChatEvent(EMPTY_CHAT, chat({ type: 'handoff', case_id: 'k', notice: 'An agent will reply.' }));
    state = applyChatEvent(state, chat({ type: 'conversation_status', status: 'closed', outcome: 'handed_off' }));
    expect(state.handoff).toEqual({ case_id: 'k', notice: 'An agent will reply.' });
    expect(state.status).toBe('closed');
    expect(state.outcome).toBe('handed_off');
  });

  it('a new customer message clears a resolved outcome', () => {
    let state = applyChatEvent(EMPTY_CHAT, chat({ type: 'conversation_status', status: 'open', outcome: 'resolved' }));
    state = applyChatEvent(state, chat({ type: 'message_created', message: msg(5, 'customer', 'one more thing') }));
    expect(state.outcome).toBeNull();
  });

  it('an error stops streaming and is shown', () => {
    let state = applyChatEvent(EMPTY_CHAT, chat({ type: 'token', message_id: 2, text: 'Hal' }));
    state = applyChatEvent(state, chat({ type: 'error', message: 'Failed' }));
    expect(state.error).toBe('Failed');
    expect(state.messages[0].streaming).toBe(false);
  });
});

const I = 'f00d0000-0000-4000-8000-000000000001';
let nextId = 1;
const ev = (type: InvestigationEvent['type'], payload: object, id = nextId++) =>
  ({ id, investigation_id: I, type, created_at: at, payload }) as InvestigationEvent;

describe('applyInvestigationEvent', () => {
  beforeEach(() => (nextId = 1));

  const evidence = { id: 'ev-1', origin: 'investigation', tool: 'get_card', summary: 'frozen', created_at: at } as const;
  const diagnosis = {
    id: 'd',
    issue_type: 'card_decline',
    root_cause: 'card_frozen_by_customer',
    explanation: 'Frozen.',
    confidence: 0.9,
    evidence_ids: ['ev-1'],
    recommended_action: 'Unfreeze',
  } as const;

  it('joins tool calls with their evidence, streams the explanation and pauses for review', () => {
    const state = timelineFromEvents([
      ev('status_changed', { status: 'running' }),
      ev('step_started', { node: 'intake' }),
      ev('tool_call', { call_id: 'x', tool: 'get_card', args: { card_id: 'c' } }),
      ev('evidence', { call_id: 'x', evidence }),
      ev('step_started', { node: 'diagnose' }),
      ev('token', { text: 'The card ' }),
      ev('token', { text: 'is frozen.' }),
      ev('diagnosis', { diagnosis }),
      ev('awaiting_review', { has_diagnosis: true }),
    ]);
    const tools = state.entries.filter((e) => e.kind === 'tool');
    expect(tools).toHaveLength(1);
    expect(tools[0].kind === 'tool' && tools[0].evidence?.id).toBe('ev-1');
    expect(state.explanation).toBe('The card is frozen.');
    expect(state.diagnosis?.root_cause).toBe('card_frozen_by_customer');
    expect(state.awaitingReview).toBe(true);
    expect(state.status).toBe('awaiting_review');
    expect(state.lastEventId).toBe(9);
    expect(state.evidence).toHaveLength(1);
  });

  it('ignores events already applied (replay after reconnect)', () => {
    const a = ev('step_started', { node: 'intake' });
    const b = ev('step_started', { node: 'triage' });
    let state = [a, b].reduce(applyInvestigationEvent, EMPTY_TIMELINE);
    state = applyInvestigationEvent(state, a);
    state = applyInvestigationEvent(state, b);
    expect(state.entries).toHaveLength(2);
  });

  it('a rejection clears the diagnosis until the resumed run produces a new one', () => {
    let state = timelineFromEvents([ev('diagnosis', { diagnosis }), ev('awaiting_review', {})]);
    state = applyInvestigationEvent(
      state,
      ev('review_recorded', { review: { verdict: 'rejected', hint: 'check limits', created_at: at } }),
    );
    expect(state.diagnosis).toBeNull();
    expect(state.awaitingReview).toBe(false);
    expect(state.review?.hint).toBe('check limits');
    state = applyInvestigationEvent(state, ev('status_changed', { status: 'running' }));
    expect(state.status).toBe('running');
  });

  it('records an escalation', () => {
    const state = timelineFromEvents([
      ev('escalated', { escalation: { summary: 'No conclusion', evidence_ids: [] } }),
      ev('awaiting_review', { has_diagnosis: false }),
    ]);
    expect(state.escalation?.summary).toBe('No conclusion');
    expect(state.diagnosis).toBeNull();
  });
});
