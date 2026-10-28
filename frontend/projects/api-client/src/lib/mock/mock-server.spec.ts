import { firstValueFrom, take, toArray } from 'rxjs';
import { ApiTransport, CustomerApi, OpsApi, SharedApi } from '../client';
import { ApiError } from '../sse';
import { timelineFromEvents } from '../timeline-state';
import { MockAgentServer } from './mock-server';
import { seededRandom } from './testing';

function setup() {
  const server = new MockAgentServer({ tickMs: 0, random: seededRandom() });
  const transport = new ApiTransport({ baseUrl: '', fetch: server.fetch, reconnectDelayMs: 0 });
  let customerId: string | null = null;
  return {
    server,
    ops: new OpsApi(transport),
    shared: new SharedApi(transport),
    customer: new CustomerApi(transport, () => customerId),
    loginAs: (id: string | null) => (customerId = id),
  };
}

async function customerNamed(shared: SharedApi, name: string) {
  const all = await shared.listCustomers();
  const found = all.find((c) => c.name.startsWith(name));
  if (!found) throw new Error(`no customer ${name}`);
  return found;
}

describe('MockAgentServer (through the real client)', () => {
  it('seeds customers and an open case queue, oldest first', async () => {
    const { shared, ops } = setup();
    expect((await shared.listCustomers()).length).toBeGreaterThanOrEqual(6);
    const page = await ops.listCases();
    expect(page.items.map((c) => [c.customer_name, c.source, c.status])).toEqual([
      ['Daniel Novak', 'assistant_handoff', 'awaiting_review'],
      ['Ben Okafor', 'email', 'awaiting_review'],
      ['Emma Lewis', 'phone', 'needs_manual'],
    ]);
    const conversations = await shared.listConversations({ outcome: 'resolved' });
    expect(conversations.items[0].customer_name).toBe('Alice Morgan');
  });

  it('requires X-Customer-Id on customer endpoints', async () => {
    const { customer, loginAs } = setup();
    await expect(customer.getMyOverview()).rejects.toThrow(/log in as/);
    loginAs('00000000-0000-4000-8000-000000000000');
    await expect(customer.getMyOverview()).rejects.toMatchObject({ status: 401 });
  });

  it('streams a self-serve answer with sources, and 👍 resolves the conversation', async () => {
    const { shared, customer, loginAs } = setup();
    const alice = await customerNamed(shared, 'Alice');
    loginAs(alice.id);
    const overview = await customer.getMyOverview();
    expect(overview.cards[0].status).toBe('frozen');

    const conv = await customer.createConversation();
    const events = await firstValueFrom(customer.sendMessage(conv.id, 'Why is my card declined?').pipe(toArray()));
    const types = events.map((e) => e.type);
    expect(types[0]).toBe('message_created');
    expect(types.filter((t) => t === 'token').length).toBeGreaterThan(5);
    expect(types.slice(-2)).toEqual(['sources', 'message_completed']);
    const done = events.at(-1)!;
    if (done.type !== 'message_completed') throw new Error('expected message_completed');
    expect(done.message.sources.some((s) => s.kind === 'article')).toBe(true);

    const rated = await customer.rateMessage(conv.id, done.message.id, 'up');
    expect(rated.rating).toBe('up');
    const detail = await shared.getConversation(conv.id);
    expect(detail.outcome).toBe('resolved');
    expect(detail.status).toBe('open');
    expect(detail.messages).toHaveLength(2);
  });

  it('rejects rating a customer message with 409', async () => {
    const { shared, customer, loginAs } = setup();
    loginAs((await customerNamed(shared, 'Alice')).id);
    const conv = await customer.createConversation();
    const events = await firstValueFrom(customer.sendMessage(conv.id, 'card declined').pipe(toArray()));
    const first = events[0];
    if (first.type !== 'message_created') throw new Error('expected message_created');
    await expect(customer.rateMessage(conv.id, first.message.id, 'up')).rejects.toMatchObject({ status: 409 });
  });

  it('hands off when asked for a human: case created, conversation closed, then 409', async () => {
    const { server, shared, customer, ops, loginAs } = setup();
    loginAs((await customerNamed(shared, 'Chloe')).id);
    const conv = await customer.createConversation();
    const events = await firstValueFrom(
      customer.sendMessage(conv.id, 'My direct debit failed, I want to talk to a person').pipe(toArray()),
    );
    expect(events.map((e) => e.type)).toEqual(['message_created', 'message_created', 'handoff', 'conversation_status']);
    const handoff = events[2];
    if (handoff.type !== 'handoff') throw new Error('expected handoff');

    await expect(firstValueFrom(customer.sendMessage(conv.id, 'hello?'))).rejects.toMatchObject({ status: 409 });

    await server.settle();
    const kase = await ops.getCase(handoff.case_id);
    expect(kase.source).toBe('assistant_handoff');
    expect(kase.conversation_id).toBe(conv.id);
    expect(kase.transcript.length).toBe(2);
    expect(kase.status).toBe('awaiting_review');
    expect(kase.investigations[0].status).toBe('awaiting_review');
  });

  it('two 👎 trigger the repeated-failure handoff on the next message', async () => {
    const { shared, customer, loginAs } = setup();
    loginAs((await customerNamed(shared, 'Emma')).id);
    const conv = await customer.createConversation();
    for (const text of ['my card?', 'still my card?']) {
      const events = await firstValueFrom(customer.sendMessage(conv.id, text).pipe(toArray()));
      const done = events.at(-1)!;
      if (done.type !== 'message_completed') throw new Error('expected an answer');
      await customer.rateMessage(conv.id, done.message.id, 'down');
    }
    const events = await firstValueFrom(customer.sendMessage(conv.id, 'and now?').pipe(toArray()));
    expect(events.map((e) => e.type)).toContain('handoff');
  });

  it('watch mode follows a simulated customer live until the conversation closes', async () => {
    const { shared, ops } = setup();
    const sim = await ops.simulateCustomer({ type: 'card_decline' });
    expect(Object.keys(sim)).not.toContain('expected_root_cause');
    const events = await firstValueFrom(shared.conversationEvents(sim.conversation_id).pipe(toArray()));
    expect(events[0].type).toBe('conversation_status'); // replay of an empty conversation
    expect(events.some((e) => e.type === 'message_created' && e.message.role === 'customer')).toBe(true);
    const last = events.at(-1)!;
    expect(last.type).toBe('conversation_status');
    if (last.type === 'conversation_status') expect(last.status).toBe('closed');

    // Reopening replays the stored messages, then closes (the conversation is closed).
    const replay = await firstValueFrom(shared.conversationEvents(sim.conversation_id).pipe(toArray()));
    expect(replay.filter((e) => e.type === 'message_created').length).toBeGreaterThanOrEqual(2);
    expect(replay.some((e) => e.type === 'token')).toBe(false);
  });

  it('streams a timeline live, then replays it as JSON and after an id', async () => {
    const { shared, ops } = setup();
    const chloe = await customerNamed(shared, 'Chloe');
    const kase = await ops.createCase({ customer_id: chloe.id, source: 'email', complaint: 'My gym direct debit failed.' });
    expect(kase.status).toBe('new');
    expect(kase.active_investigation_id).toBeTruthy();
    const id = kase.active_investigation_id!;

    const live = await firstValueFrom(ops.streamInvestigationEvents(id).pipe(toArray()));
    const types = live.map((e) => e.type);
    expect(types[0]).toBe('status_changed');
    expect(types).toContain('classified');
    expect(types).toContain('tool_call');
    expect(types).toContain('evidence');
    expect(types).toContain('token');
    expect(types).toContain('diagnosis');
    expect(types.at(-1)).toBe('awaiting_review');
    const ids = live.map((e) => e.id);
    expect([...ids].sort((a, b) => a - b)).toEqual(ids);

    const stored = await ops.getInvestigationEvents(id);
    expect(stored.map((e) => e.id)).toEqual(ids);
    const after = await firstValueFrom(ops.streamInvestigationEvents(id, ids[4]).pipe(toArray()));
    expect(after.map((e) => e.id)).toEqual(ids.slice(5));

    const timeline = timelineFromEvents(live);
    expect(timeline.diagnosis?.root_cause).toBe('mandate_cancelled_by_customer');
    const inv = await ops.getInvestigation(id);
    expect(inv.status).toBe('awaiting_review');
    expect(inv.diagnosis?.evidence_ids.every((e) => inv.evidence.some((x) => x.id === e))).toBe(true);
  });

  it('reject with hint resumes the same investigation; accept resolves the case', async () => {
    const { server, ops } = setup();
    const [daniel] = (await ops.listCases()).items;
    const id = daniel.active_investigation_id!;
    const before = await ops.getInvestigationEvents(id);

    await expect(ops.submitFeedback(id, { verdict: 'rejected' })).rejects.toMatchObject({ status: 400 });
    await expect(ops.submitFeedback(id, { verdict: 'corrected' })).rejects.toMatchObject({ status: 400 });

    const resumed = await ops.submitFeedback(id, { verdict: 'rejected', hint: 'Check if the card was reported lost' });
    expect(resumed.status).toBe('running');
    expect(resumed.id).toBe(id);
    const more = await firstValueFrom(ops.streamInvestigationEvents(id, before.at(-1)!.id).pipe(toArray()));
    expect(more[0].type).toBe('review_recorded');
    const diagnosis = more.find((e) => e.type === 'diagnosis');
    expect(diagnosis && diagnosis.type === 'diagnosis' && diagnosis.payload.diagnosis.root_cause).toBe('card_lost_or_stolen');
    expect(more.at(-1)!.type).toBe('awaiting_review');

    const accepted = await ops.submitFeedback(id, { verdict: 'accepted' });
    expect(accepted.status).toBe('completed');
    expect(accepted.verdict).toBe('accepted');
    const kase = await ops.getCase(daniel.id);
    expect(kase.status).toBe('resolved');
    expect(kase.active_investigation_id).toBeNull();
    await expect(ops.submitFeedback(id, { verdict: 'accepted' })).rejects.toMatchObject({ status: 409 });
    await expect(ops.investigateCase(daniel.id)).rejects.toMatchObject({ status: 409 });
    await server.settle();
  });

  it('re-investigate supersedes a run awaiting review and starts a new one', async () => {
    const { server, ops } = setup();
    const ben = (await ops.listCases()).items[1];
    const old = ben.active_investigation_id!;
    const fresh = await ops.reinvestigateCase(ben.id);
    expect(fresh.id).not.toBe(old);
    expect(fresh.status).toBe('queued');
    expect((await ops.getInvestigation(old)).status).toBe('superseded');
    // One active investigation per case: a second Re-investigate is rejected, Investigate returns it.
    await expect(ops.reinvestigateCase(ben.id)).rejects.toMatchObject({ status: 409 });
    expect((await ops.investigateCase(ben.id)).id).toBe(fresh.id);
    await server.settle();
    const detail = await ops.getCase(ben.id);
    expect(detail.investigations.map((i) => i.status)).toEqual(['awaiting_review', 'superseded']);
  });

  it('a parked case is investigated manually and escalates; accepting resolves it', async () => {
    const { server, ops } = setup();
    const emma = (await ops.listCases()).items[2];
    expect(emma.status).toBe('needs_manual');
    const parked = await ops.getInvestigation((await ops.getCase(emma.id)).investigations[0].id);
    expect(parked.status).toBe('completed');
    expect(parked.diagnosis).toBeNull();

    const manual = await ops.investigateCase(emma.id);
    expect(manual.trigger).toBe('manual');
    await server.settle();
    const inv = await ops.getInvestigation(manual.id);
    expect(inv.status).toBe('awaiting_review');
    expect(inv.escalation?.summary).toBeTruthy();
    expect((await ops.getCase(emma.id)).status).toBe('escalated');
    await ops.submitFeedback(manual.id, { verdict: 'accepted', notes: 'Explained the fee by phone.' });
    expect((await ops.getCase(emma.id)).status).toBe('resolved');
  });

  it('publishes case queue events live', async () => {
    const { server, ops } = setup();
    const received = firstValueFrom(ops.caseEvents().pipe(take(3), toArray()));
    await new Promise((r) => setTimeout(r, 0));
    const kase = await ops.simulateEmail({ type: 'missed_payment' });
    const events = await received;
    expect(events[0]).toMatchObject({ type: 'case_created', case: { id: kase.id, source: 'simulated_email' } });
    expect(events[1]).toMatchObject({ type: 'status_changed', case_id: kase.id, previous_status: 'new', status: 'investigating' });
    await server.settle();
  });

  it('validates requests with problem details', async () => {
    const { ops } = setup();
    await expect(ops.simulateEmail({ type: 'other' })).rejects.toMatchObject({ status: 400 });
    await expect(ops.listCases({ status: ['bogus' as never] })).rejects.toBeInstanceOf(ApiError);
    await expect(ops.getCase('00000000-0000-4000-8000-000000000000')).rejects.toMatchObject({ status: 404 });
    await expect(
      ops.createCase({ customer_id: '00000000-0000-4000-8000-000000000000', source: 'phone', complaint: 'x' }),
    ).rejects.toMatchObject({ status: 404 });
    const [first] = await new SharedApi(new ApiTransport({ baseUrl: '', fetch: setup().server.fetch })).listCustomers();
    await expect(ops.createCase({ customer_id: first.id, source: 'phone' })).rejects.toMatchObject({ status: 400 });
  });
});
