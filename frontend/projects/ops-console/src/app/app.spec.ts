import { provideRouter, withComponentInputBinding } from '@angular/router';
import { RouterTestingHarness } from '@angular/router/testing';
import { MockAgentServer, OpsApi, SharedApi } from '@poc/api-client';
import { opsConsoleProviders } from './app.config';
import { routes } from './app.routes';
import { TestBed, all, el, mockServer, text, typeInto, waitFor } from './testing';

describe('ops console', () => {
  let server: MockAgentServer;
  let harness: RouterTestingHarness;

  beforeEach(async () => {
    server = mockServer();
    TestBed.configureTestingModule({
      providers: [
        provideRouter(routes, withComponentInputBinding()),
        ...opsConsoleProviders({ mode: 'mock', baseUrl: '', mockServer: server, reconnectDelayMs: 0 }, 'http://customer.test'),
      ],
    });
    harness = await RouterTestingHarness.create();
  });

  afterEach(() => server.settle());

  const rows = () => all(harness.fixture, 'tbody tr[data-case-id]');
  const select = (selector: string, value: string) => {
    const s = el<HTMLSelectElement>(harness.fixture, selector);
    s.value = value;
    s.dispatchEvent(new Event('change'));
  };

  async function caseOf(name: string) {
    const page = await TestBed.inject(OpsApi).listCases();
    return page.items.find((c) => c.customer_name?.startsWith(name))!;
  }

  it('lists the open cases with source, category and status', async () => {
    await harness.navigateByUrl('/cases');
    await waitFor(harness.fixture, () => rows().length === 3);
    const first = rows()[0].textContent!;
    expect(first).toContain('Daniel Novak');
    expect(first).toContain('Assistant handoff');
    expect(first).toContain('Card decline');
    expect(first).toContain('Awaiting review');
  });

  it('Simulate email complaint adds a case live, and its status follows the run', async () => {
    await harness.navigateByUrl('/cases');
    await waitFor(harness.fixture, () => rows().length === 3);
    select('#scenario', 'failed_direct_debit');
    el<HTMLButtonElement>(harness.fixture, 'button.sim-email').click();
    await waitFor(harness.fixture, () => rows().length === 4);
    expect(rows()[3].textContent).toContain('Simulated email');
    expect(rows()[3].textContent).toContain('simulated');
    await waitFor(harness.fixture, () => rows()[3].textContent!.includes('Awaiting review'));
    expect(rows()[3].textContent).toContain('Failed direct debit');
  });

  it('Simulate customer links to watch mode in the customer app', async () => {
    await harness.navigateByUrl('/cases');
    await waitFor(harness.fixture, () => rows().length === 3);
    el<HTMLButtonElement>(harness.fixture, 'button.sim-customer').click();
    await waitFor(harness.fixture, () => !!harness.fixture.nativeElement.querySelector('.notice a'));
    const link = el<HTMLAnchorElement>(harness.fixture, '.notice a');
    expect(link.href).toMatch(/^http:\/\/customer\.test\/watch\/[0-9a-f-]{36}$/);
  });

  it('new case form creates a case and opens it', async () => {
    const customers = await TestBed.inject(SharedApi).listCustomers();
    const chloe = customers.find((c) => c.name.startsWith('Chloe'))!;
    await harness.navigateByUrl('/cases/new');
    await waitFor(harness.fixture, () => all(harness.fixture, '#customer option').length > 1);
    const submit = el<HTMLButtonElement>(harness.fixture, 'button[type=submit]');
    expect(submit.disabled).toBe(true);
    select('#customer', chloe.id);
    select('#source', 'phone');
    typeInto(el<HTMLTextAreaElement>(harness.fixture, '#complaint'), 'Gym direct debit bounced.');
    harness.fixture.detectChanges();
    expect(submit.disabled).toBe(false);
    submit.click();
    await waitFor(harness.fixture, () => text(harness.fixture).includes('Gym direct debit bounced.'));
    expect(text(harness.fixture)).toContain('Chloe Davies');
    expect(text(harness.fixture)).toContain('Phone');
    // The automatic run shows up, then pauses with a diagnosis for review.
    await waitFor(harness.fixture, () => text(harness.fixture).includes('Submit review'));
    expect(text(harness.fixture)).toContain('Mandate cancelled by customer');
  });

  it('case detail: transcript, timeline, diagnosis with linked evidence; accept resolves', async () => {
    const daniel = await caseOf('Daniel');
    await harness.navigateByUrl(`/cases/${daniel.id}`);
    await waitFor(harness.fixture, () => text(harness.fixture).includes('Submit review'));
    const page = text(harness.fixture);
    expect(page).toContain('Conversation transcript');
    expect(page).toContain('Assistant\'s evidence');
    expect(page).toContain('Classified as Card decline');
    expect(page).toContain('get_fraud_flags');
    expect(page).toContain('Fraud rule triggered');
    expect(page).toContain('Paused for review');

    const evLink = all<HTMLButtonElement>(harness.fixture, '.ev-link')[0];
    evLink.click();
    harness.fixture.detectChanges();
    expect(el(harness.fixture, '.result.hl').textContent).toContain(evLink.textContent!.trim());

    el<HTMLButtonElement>(harness.fixture, 'button[type=submit]').click();
    await waitFor(harness.fixture, () => el(harness.fixture, '.case-status').textContent!.includes('Resolved'));
    expect(text(harness.fixture)).not.toContain('Submit review');
    expect(text(harness.fixture)).toContain('accepted');
    expect(el<HTMLButtonElement>(harness.fixture, 'button.reinvestigate').disabled).toBe(true);
  });

  it('reject needs a hint, then the same run resumes live with a new diagnosis', async () => {
    const ben = await caseOf('Ben');
    await harness.navigateByUrl(`/cases/${ben.id}`);
    await waitFor(harness.fixture, () => text(harness.fixture).includes('Submit review'));
    expect(text(harness.fixture)).toContain('Insufficient funds');
    const reject = all<HTMLInputElement>(harness.fixture, 'input[name=verdict]').find((r) => r.value === 'rejected')!;
    reject.click();
    reject.dispatchEvent(new Event('change'));
    harness.fixture.detectChanges();
    await harness.fixture.whenStable();
    const submit = el<HTMLButtonElement>(harness.fixture, 'button[type=submit]');
    expect(submit.disabled).toBe(true);
    typeInto(el<HTMLTextAreaElement>(harness.fixture, '#hint'), 'Check the daily limit');
    harness.fixture.detectChanges();
    expect(submit.disabled).toBe(false);
    submit.click();

    await waitFor(harness.fixture, () => text(harness.fixture).includes('Daily payment limit exceeded') && text(harness.fixture).includes('Submit review'));
    const page = text(harness.fixture);
    expect(page).toContain('hint: "Check the daily limit"');
    expect(page).toContain('Re-checked recent activity');
    // Still one investigation: the reject resumed the same one.
    expect(all(harness.fixture, '.inv-row')).toHaveLength(0);
  });

  it('Re-investigate starts a new run and lists the superseded one', async () => {
    const ben = await caseOf('Ben');
    await harness.navigateByUrl(`/cases/${ben.id}`);
    await waitFor(harness.fixture, () => text(harness.fixture).includes('Submit review'));
    el<HTMLButtonElement>(harness.fixture, 'button.reinvestigate').click();
    await waitFor(harness.fixture, () => all(harness.fixture, '.inv-row').length === 2);
    await waitFor(harness.fixture, () => text(harness.fixture).includes('Submit review'));
    const statuses = all(harness.fixture, '.inv-row').map((r) => r.textContent!);
    expect(statuses[1]).toContain('Superseded');
    all<HTMLElement>(harness.fixture, '.inv-row')[1].click();
    await waitFor(harness.fixture, () => text(harness.fixture).includes('Status: Superseded'));
  });

  it('a parked case can be investigated manually and escalates', async () => {
    const emma = await caseOf('Emma');
    await harness.navigateByUrl(`/cases/${emma.id}`);
    await waitFor(harness.fixture, () => text(harness.fixture).includes('Needs manual'));
    expect(text(harness.fixture)).toContain('Park');
    el<HTMLButtonElement>(harness.fixture, 'button.investigate').click();
    await waitFor(harness.fixture, () => text(harness.fixture).includes('Escalated') && text(harness.fixture).includes('Submit review'));
    expect(el(harness.fixture, '.case-status').textContent).toContain('Escalated');
  });

  it('conversations list filters by outcome and opens a transcript', async () => {
    await harness.navigateByUrl('/conversations');
    await waitFor(harness.fixture, () => all(harness.fixture, 'tbody tr.clickable').length === 2);
    select('#outcome', 'handed_off');
    await waitFor(harness.fixture, () => all(harness.fixture, 'tbody tr.clickable').length === 1);
    expect(el(harness.fixture, 'tbody tr').textContent).toContain('Daniel Novak');
    el<HTMLElement>(harness.fixture, 'tbody tr.clickable').click();
    await waitFor(harness.fixture, () => text(harness.fixture).includes('Open case'));
    expect(text(harness.fixture)).toContain("I've passed this to our support team");
  });
});
