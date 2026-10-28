// Checks the mock against contracts/agent-api.yaml: every response the mock gives during a
// full tour of both apps' flows must use a status code and content type the contract
// documents for that operation, and its body (each SSE event's data, for streams) must
// validate against the documented schema. Every operation must be exercised.

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import Ajv2020 from 'ajv/dist/2020';
import addFormats from 'ajv-formats';
import { firstValueFrom, take, toArray } from 'rxjs';
import { parse } from 'yaml';
import { ApiTransport, CustomerApi, OpsApi, SharedApi } from '../client';
import { FetchFn, readSse } from '../sse';
import { MockAgentServer } from './mock-server';
import { seededRandom } from './testing';

const contracts = (file: string) => fileURLToPath(new URL(`../../../../../../contracts/${file}`, import.meta.url));
const SPEC_ID = 'https://claude-poc.local/contracts/agent-api.yaml';

interface Operation {
  responses: Record<string, { $ref?: string; content?: Record<string, { schema: unknown }> }>;
}
type Spec = { paths: Record<string, Record<string, Operation>>; components: { responses: Record<string, Operation['responses'][string]> } };

const spec = parse(readFileSync(contracts('agent-api.yaml'), 'utf8')) as Spec & { $id?: string };
const scenario = JSON.parse(readFileSync(contracts('scenario.schema.json'), 'utf8'));

const ajv = new Ajv2020({ strict: false, allErrors: true });
addFormats(ajv);
ajv.addFormat('int64', true);
ajv.addSchema(scenario);
ajv.addSchema({ ...spec, $id: SPEC_ID });

const pointer = (...parts: string[]) => parts.map((p) => p.replace(/~/g, '~0').replace(/\//g, '~1')).join('/');

const routes = Object.keys(spec.paths).map((template) => ({
  template,
  regex: new RegExp(`^${template.replace(/\{[^}]+\}/g, '[^/]+')}$`),
}));

function findRoute(path: string) {
  // Prefer literal segments: /cases/events over /cases/{case_id}.
  return routes
    .filter((r) => r.regex.test(path))
    .sort((a, b) => (a.template.match(/\{/g)?.length ?? 0) - (b.template.match(/\{/g)?.length ?? 0))[0];
}

const failures: string[] = [];
const exercised = new Set<string>();

function validate(where: string, schemaRef: string, data: unknown) {
  const validator = ajv.getSchema(schemaRef) ?? ajv.compile({ $ref: schemaRef });
  if (!validator(data)) {
    failures.push(`${where}: ${ajv.errorsText(validator.errors)}\n  ${JSON.stringify(data).slice(0, 300)}`);
  }
}

function findExpectedKey(value: unknown, path = '$'): string | null {
  if (Array.isArray(value)) {
    for (let i = 0; i < value.length; i++) {
      const hit = findExpectedKey(value[i], `${path}[${i}]`);
      if (hit) return hit;
    }
  } else if (value && typeof value === 'object') {
    for (const [k, v] of Object.entries(value)) {
      if (k.startsWith('expected_')) return `${path}.${k}`;
      const hit = findExpectedKey(v, `${path}.${k}`);
      if (hit) return hit;
    }
  }
  return null;
}

/** Wraps the mock's fetch and checks each response against the contract. */
function checkingFetch(inner: FetchFn): FetchFn {
  return async (input, init) => {
    const res = await inner(input, init);
    const url = new URL(input, 'http://mock.local');
    const method = (init?.method ?? 'GET').toLowerCase();
    const where = `${method.toUpperCase()} ${url.pathname} -> ${res.status}`;
    const route = findRoute(url.pathname);
    const op = route && spec.paths[route.template][method];
    if (!route || !op) {
      failures.push(`${where}: no such operation in the contract`);
      return res;
    }
    exercised.add(`${method.toUpperCase()} ${route.template}`);
    let response = op.responses[String(res.status)];
    let base = pointer('paths', route.template, method, 'responses', String(res.status));
    if (!response) {
      failures.push(`${where}: status not documented`);
      return res;
    }
    if (response.$ref) {
      const name = response.$ref.split('/').pop()!;
      response = spec.components.responses[name];
      base = pointer('components', 'responses', name);
    }
    const contentType = (res.headers.get('Content-Type') ?? '').split(';')[0];
    if (!response.content?.[contentType]) {
      failures.push(`${where}: content type ${contentType} not documented`);
      return res;
    }
    const schemaRef = `${SPEC_ID}#/${base}/${pointer('content', contentType, 'schema')}`;
    const copy = res.clone();
    if (contentType === 'text/event-stream') {
      (async () => {
        try {
          for await (const msg of readSse(copy.body!)) {
            const data = JSON.parse(msg.data);
            if (data.type !== msg.event) failures.push(`${where}: SSE event name ${msg.event} != type ${data.type}`);
            const leak = findExpectedKey(data);
            if (leak) failures.push(`${where}: answer key exposed at ${leak}`);
            validate(`${where} [${msg.event}]`, schemaRef, data);
          }
        } catch {
          // the client aborted the stream
        }
      })();
    } else {
      const data = await copy.json();
      const leak = findExpectedKey(data);
      if (leak) failures.push(`${where}: answer key exposed at ${leak}`);
      validate(where, schemaRef, data);
    }
    return res;
  };
}

describe('mock agent service vs contracts/agent-api.yaml', () => {
  it('the checker rejects a body that breaks the contract', () => {
    const before = failures.length;
    validate('self-test', `${SPEC_ID}#/components/schemas/Case`, { id: 'x', status: 'bogus' });
    validate('self-test', `${SPEC_ID}#/${pointer('paths', '/cases', 'get', 'responses', '200', 'content', 'application/json', 'schema')}`, {
      items: [{ id: 1 }],
    });
    expect(failures.length).toBe(before + 2);
    failures.length = before;
  });

  it('every response matches the documented status, content type and schema', async () => {
    const server = new MockAgentServer({ tickMs: 0, random: seededRandom(7) });
    const transport = new ApiTransport({ baseUrl: '', fetch: checkingFetch(server.fetch), reconnectDelayMs: 0 });
    let customerId: string | null = null;
    const customer = new CustomerApi(transport, () => customerId);
    const shared = new SharedApi(transport);
    const ops = new OpsApi(transport);
    const swallow = (p: Promise<unknown>) => p.catch(() => undefined);

    await transport.json('GET', '/health');

    // Customer app: log in as, account view, chat (answer, rating, handoff), watch mode.
    const customers = await shared.listCustomers();
    const alice = customers.find((c) => c.name.startsWith('Alice'))!;
    const chloe = customers.find((c) => c.name.startsWith('Chloe'))!;
    customerId = 'not-a-customer';
    await swallow(customer.getMyOverview());
    customerId = alice.id;
    await customer.getMyOverview();
    const conv = await customer.createConversation();
    const answer = await firstValueFrom(customer.sendMessage(conv.id, 'Why is my card declined?').pipe(toArray()));
    const completed = answer.find((e) => e.type === 'message_completed')!;
    if (completed.type !== 'message_completed') throw new Error('no answer');
    await customer.rateMessage(conv.id, completed.message.id, 'up');
    await swallow(customer.rateMessage(conv.id, answer[0].type === 'message_created' ? answer[0].message.id : 0, 'up'));
    await swallow(customer.rateMessage(conv.id, 999999, 'up'));
    customerId = chloe.id;
    const conv2 = await customer.createConversation();
    await firstValueFrom(customer.sendMessage(conv2.id, 'Direct debit failed, I need a person').pipe(toArray()));
    await swallow(firstValueFrom(customer.sendMessage(conv2.id, 'hello?')));
    await swallow(firstValueFrom(customer.sendMessage('00000000-0000-4000-8000-000000000000', 'hi')));

    const sim = await ops.simulateCustomer({ type: 'missed_payment' });
    await firstValueFrom(shared.conversationEvents(sim.conversation_id).pipe(toArray()));
    await shared.listConversations({ simulated: true, status: 'closed' });
    await shared.getConversation(conv.id);
    await swallow(shared.getConversation('00000000-0000-4000-8000-000000000000'));
    await swallow(shared.listConversations({ limit: 0 }));

    // Ops console: queue, live updates, new case, simulate, detail, timeline, review.
    const queueEvents = firstValueFrom(ops.caseEvents().pipe(take(2), toArray()));
    await new Promise((r) => setTimeout(r, 0));
    const created = await ops.createCase({ customer_id: alice.id, source: 'phone', complaint: 'Card refused at Tesco' });
    await queueEvents;
    await swallow(ops.createCase({ customer_id: alice.id, source: 'phone' } as never));
    await swallow(ops.createCase({ customer_id: '00000000-0000-4000-8000-000000000000', source: 'email', complaint: 'x' }));
    const email = await ops.simulateEmail({ type: 'failed_direct_debit' });
    await swallow(ops.simulateEmail({ type: 'other' }));
    await ops.listCases({ status: ['awaiting_review', 'escalated'], limit: 10 });
    await swallow(ops.listCases({ status: ['nope' as never] }));

    const invId = created.active_investigation_id!;
    await firstValueFrom(ops.streamInvestigationEvents(invId).pipe(toArray()));
    await ops.getInvestigationEvents(invId, 2);
    await ops.getInvestigation(invId);
    await swallow(ops.getInvestigation('00000000-0000-4000-8000-000000000000'));
    await ops.getCase(created.id);
    await swallow(ops.getCase('00000000-0000-4000-8000-000000000000'));
    await swallow(ops.submitFeedback(invId, { verdict: 'rejected' }));
    await ops.submitFeedback(invId, { verdict: 'rejected', hint: 'Look at the merchant category' });
    await swallow(ops.reinvestigateCase(created.id)); // 409 while running
    await firstValueFrom(ops.streamInvestigationEvents(invId).pipe(toArray()));
    await ops.investigateCase(created.id); // 200: the active one
    const again = await ops.reinvestigateCase(created.id); // supersedes
    await firstValueFrom(ops.streamInvestigationEvents(again.id).pipe(toArray()));
    await ops.submitFeedback(again.id, { verdict: 'corrected', corrected_root_cause: 'card_frozen_by_customer' });
    await swallow(ops.submitFeedback(again.id, { verdict: 'accepted' })); // 409
    await swallow(ops.investigateCase(created.id)); // 409 resolved
    await swallow(ops.reinvestigateCase('00000000-0000-4000-8000-000000000000'));

    const parked = (await ops.listCases({ status: ['needs_manual'] })).items[0];
    const manual = await ops.investigateCase(parked.id); // 201
    await firstValueFrom(ops.streamInvestigationEvents(manual.id).pipe(toArray()));
    await ops.getCase(parked.id);
    await ops.getInvestigation(manual.id);
    await ops.submitFeedback(manual.id, { verdict: 'accepted' });

    await ops.getCase(email.id);
    await server.settle();
    await ops.listCases({ status: ['resolved'] });
    await new Promise((r) => setTimeout(r, 10));

    expect(failures).toEqual([]);

    const all = Object.entries(spec.paths).flatMap(([path, ops]) =>
      Object.keys(ops)
        .filter((m) => ['get', 'post', 'put', 'patch', 'delete'].includes(m))
        .map((m) => `${m.toUpperCase()} ${path}`),
    );
    expect(all.filter((op) => !exercised.has(op))).toEqual([]);
  });
});
