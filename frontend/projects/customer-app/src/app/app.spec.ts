import { provideRouter, withComponentInputBinding } from '@angular/router';
import { RouterTestingHarness } from '@angular/router/testing';
import { CustomerSummary, MockAgentServer, OpsApi, SharedApi } from '@poc/api-client';
import { customerAppProviders } from './app.config';
import { routes } from './app.routes';
import { CustomerSession } from './session';
import { TestBed, all, el, mockServer, text, typeInto, waitFor } from './testing';

describe('customer app', () => {
  let server: MockAgentServer;
  let harness: RouterTestingHarness;

  beforeEach(async () => {
    sessionStorage.clear();
    server = mockServer();
    TestBed.configureTestingModule({
      providers: [
        provideRouter(routes, withComponentInputBinding()),
        ...customerAppProviders({ mode: 'mock', baseUrl: '', mockServer: server, reconnectDelayMs: 0 }),
      ],
    });
    harness = await RouterTestingHarness.create();
  });

  afterEach(() => server.settle());

  async function customer(name: string): Promise<CustomerSummary> {
    const list = await TestBed.inject(SharedApi).listCustomers();
    return list.find((c) => c.name.startsWith(name))!;
  }

  async function logInAs(name: string) {
    TestBed.inject(CustomerSession).logIn(await customer(name));
  }

  it('redirects to "log in as" when nobody is logged in', async () => {
    await harness.navigateByUrl('/chat');
    await waitFor(harness.fixture, () => text(harness.fixture).includes('Alice Morgan'));
    expect(TestBed.inject(CustomerSession).customer()).toBeNull();
  });

  it('logs in as a seeded customer and shows the account view', async () => {
    await harness.navigateByUrl('/login');
    await waitFor(harness.fixture, () => all(harness.fixture, '.list-button').length >= 6);
    all<HTMLButtonElement>(harness.fixture, '.list-button')
      .find((b) => b.textContent?.includes('Alice Morgan'))!
      .click();
    await waitFor(harness.fixture, () => text(harness.fixture).includes('Card ending'));
    expect(TestBed.inject(CustomerSession).customer()?.name).toBe('Alice Morgan');
    expect(el(harness.fixture, '.card-row .badge').textContent).toContain('frozen');
    expect(text(harness.fixture)).toContain('Recent transactions');
  });

  it('chat: streams the answer with sources, then 👍 resolves it', async () => {
    await logInAs('Alice');
    await harness.navigateByUrl('/chat');
    harness.fixture.detectChanges();
    typeInto(el<HTMLInputElement>(harness.fixture, 'input[name=draft]'), 'Why was my card declined?');
    el<HTMLFormElement>(harness.fixture, 'form').dispatchEvent(new Event('submit'));

    await waitFor(harness.fixture, () => all(harness.fixture, '.thumb').length === 2);
    const assistant = el(harness.fixture, '.msg.assistant');
    expect(assistant.textContent).toContain('frozen');
    expect(all(harness.fixture, '.msg.customer')[0].textContent).toContain('Why was my card declined?');
    const link = el<HTMLAnchorElement>(harness.fixture, 'a.source');
    expect(link.textContent).toContain('Why cards get declined');
    expect(link.getAttribute('href')).toBe('/help/why-cards-get-declined');

    el<HTMLButtonElement>(harness.fixture, '.thumb[aria-label=Helpful]').click();
    await waitFor(harness.fixture, () => !!harness.fixture.nativeElement.querySelector('.thumb.on'));
    expect(text(harness.fixture)).toContain('Resolved');
    const id = TestBed.inject(CustomerSession).conversationId()!;
    const detail = await TestBed.inject(SharedApi).getConversation(id);
    expect(detail.outcome).toBe('resolved');
    expect(detail.messages.find((m) => m.role === 'assistant')?.rating).toBe('up');
  });

  it('chat: a handoff shows the notice and closes the conversation', async () => {
    await logInAs('Daniel');
    await harness.navigateByUrl('/chat');
    harness.fixture.detectChanges();
    typeInto(el<HTMLInputElement>(harness.fixture, 'input[name=draft]'), 'My card is blocked, can I talk to someone?');
    el<HTMLFormElement>(harness.fixture, 'form').dispatchEvent(new Event('submit'));
    await waitFor(harness.fixture, () => text(harness.fixture).includes('This conversation is closed'));
    expect(el(harness.fixture, '.handoff').textContent).toContain('An agent will look into this');
    expect(harness.fixture.nativeElement.querySelector('form')).toBeNull();
  });

  it('chat: reloads the conversation in progress after navigating away', async () => {
    await logInAs('Alice');
    await harness.navigateByUrl('/chat');
    harness.fixture.detectChanges();
    typeInto(el<HTMLInputElement>(harness.fixture, 'input[name=draft]'), 'card declined?');
    el<HTMLFormElement>(harness.fixture, 'form').dispatchEvent(new Event('submit'));
    await waitFor(harness.fixture, () => all(harness.fixture, '.thumb').length === 2);
    await harness.navigateByUrl('/account');
    await harness.navigateByUrl('/chat');
    await waitFor(harness.fixture, () => all(harness.fixture, '.msg').length === 2);
  });

  it('watch mode follows a simulated conversation live', async () => {
    const ops = TestBed.inject(OpsApi);
    const sim = await ops.simulateCustomer({ type: 'missed_payment' });
    await harness.navigateByUrl('/watch');
    await waitFor(harness.fixture, () => text(harness.fixture).includes(sim.customer_name!));
    el<HTMLAnchorElement>(harness.fixture, 'a.item').click();
    await waitFor(harness.fixture, () => all(harness.fixture, '.msg.customer').length === 1);
    await waitFor(harness.fixture, () => !text(harness.fixture).includes('Live'));
    expect(all(harness.fixture, '.msg.assistant').length).toBeGreaterThanOrEqual(1);
    expect(text(harness.fixture)).toMatch(/Resolved|Handed off/);
  });
});
