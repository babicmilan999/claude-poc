import { Component, OnDestroy, OnInit, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { Router, RouterLink } from '@angular/router';
import {
  ApiError,
  Case,
  CaseEvent,
  ISSUE_TYPES,
  IssueType,
  OpsApi,
  SimulatedCustomer,
  age,
  errorMessage,
  humanize,
} from '@poc/api-client';
import { Subscription } from 'rxjs';
import { CUSTOMER_APP_URL } from '../app.config';

/** Open cases, live; the Simulate buttons and New case. */
@Component({
  selector: 'ops-case-queue',
  imports: [FormsModule, RouterLink],
  template: `
    <h1>Case queue</h1>
    <div class="toolbar">
      <label class="inline" for="scenario">Scenario</label>
      <select id="scenario" name="scenario" [(ngModel)]="scenarioType">
        <option value="">Random</option>
        @for (t of simulatable; track t) {
          <option [value]="t">{{ label(t) }}</option>
        }
      </select>
      <button type="button" class="sim-customer" (click)="simulateCustomer()" [disabled]="busy()">Simulate customer</button>
      <button type="button" class="sim-email" (click)="simulateEmail()" [disabled]="busy()">Simulate email complaint</button>
      <span class="spacer"></span>
      <label class="inline"><input type="checkbox" name="showResolved" [(ngModel)]="showResolved" (ngModelChange)="load()" /> Show resolved</label>
      <a routerLink="/cases/new" class="button primary">New case</a>
    </div>

    @if (simulated(); as sim) {
      <p class="notice">
        Simulated customer <strong>{{ sim.customer_name }}</strong> is chatting with the assistant.
        <a [href]="watchUrl(sim)" target="_blank" rel="noopener">Watch it live in the customer app</a>.
        A case appears here if the assistant hands off.
      </p>
    }
    @if (error()) {
      <p class="error">{{ error() }}</p>
    }

    <table class="grid">
      <thead>
        <tr><th>Customer</th><th>Source</th><th>Category</th><th>Age</th><th>Status</th></tr>
      </thead>
      <tbody>
        @for (c of cases(); track c.id) {
          <tr class="clickable" (click)="open(c)" [attr.data-case-id]="c.id">
            <td>
              <a [routerLink]="['/cases', c.id]" (click)="$event.stopPropagation()">{{ c.customer_name || c.customer_id }}</a>
              @if (c.simulated) { <span class="badge sim">simulated</span> }
            </td>
            <td>{{ label(c.source) }}</td>
            <td>{{ c.category ? label(c.category) : '-' }}</td>
            <td>{{ ageOf(c.created_at) }}</td>
            <td><span class="badge status" [class]="c.status">{{ label(c.status) }}</span></td>
          </tr>
        } @empty {
          <tr><td colspan="5" class="muted">{{ loading() ? 'Loading...' : 'No open cases.' }}</td></tr>
        }
      </tbody>
    </table>
  `,
  styles: `.inline { display: inline-flex; gap: 4px; align-items: center; margin: 0; font-weight: normal; }`,
})
export class CaseQueuePage implements OnInit, OnDestroy {
  private readonly ops = inject(OpsApi);
  private readonly router = inject(Router);
  private readonly customerAppUrl = inject(CUSTOMER_APP_URL);
  private events?: Subscription;
  private ticker?: ReturnType<typeof setInterval>;

  readonly cases = signal<Case[]>([]);
  readonly loading = signal(true);
  readonly busy = signal(false);
  readonly error = signal<string | null>(null);
  readonly simulated = signal<SimulatedCustomer | null>(null);
  readonly now = signal(Date.now());
  readonly simulatable = ISSUE_TYPES.filter((t) => t !== 'other');
  readonly label = humanize;
  scenarioType: IssueType | '' = '';
  showResolved = false;

  ngOnInit(): void {
    this.load();
    // Live updates; after a reconnect the queue is reloaded (the stream has no replay).
    this.events = this.ops.caseEvents(() => this.load()).subscribe({
      next: (event) => this.apply(event),
      error: (err) => this.error.set(`Live updates stopped: ${errorMessage(err)}`),
    });
    this.ticker = setInterval(() => this.now.set(Date.now()), 30_000);
  }

  ngOnDestroy(): void {
    this.events?.unsubscribe();
    clearInterval(this.ticker);
  }

  async load(): Promise<void> {
    try {
      const page = await this.ops.listCases(this.showResolved ? { status: ['new', 'investigating', 'awaiting_review', 'needs_manual', 'escalated', 'resolved'] } : {});
      this.cases.set(page.items);
      this.error.set(null);
    } catch (err) {
      this.error.set(errorMessage(err));
    } finally {
      this.loading.set(false);
    }
  }

  private apply(event: CaseEvent): void {
    if (event.type === 'case_created') {
      this.cases.update((list) => (list.some((c) => c.id === event.case.id) ? list : [...list, event.case]));
      return;
    }
    this.cases.update((list) =>
      list
        .map((c) =>
          c.id === event.case_id
            ? { ...c, status: event.status, category: event.category ?? c.category, updated_at: event.created_at }
            : c,
        )
        .filter((c) => this.showResolved || c.status !== 'resolved'),
    );
  }

  ageOf(iso: string): string {
    return age(iso, this.now());
  }

  open(c: Case): void {
    this.router.navigate(['/cases', c.id]);
  }

  watchUrl(sim: SimulatedCustomer): string {
    return `${this.customerAppUrl.replace(/\/$/, '')}/watch/${sim.conversation_id}`;
  }

  async simulateCustomer(): Promise<void> {
    await this.run(async () => this.simulated.set(await this.ops.simulateCustomer(this.request())));
  }

  async simulateEmail(): Promise<void> {
    await this.run(async () => {
      await this.ops.simulateEmail(this.request());
    });
  }

  private request() {
    return this.scenarioType ? { type: this.scenarioType } : {};
  }

  private async run(task: () => Promise<void>): Promise<void> {
    this.busy.set(true);
    this.error.set(null);
    try {
      await task();
    } catch (err) {
      this.error.set(
        err instanceof ApiError && err.status === 404
          ? 'Simulation is turned off in the agent service (SIMULATION_ENABLED=false).'
          : errorMessage(err),
      );
    } finally {
      this.busy.set(false);
    }
  }
}
