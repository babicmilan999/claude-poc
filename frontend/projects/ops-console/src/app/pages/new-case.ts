import { Component, OnInit, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { Router } from '@angular/router';
import { CreateCaseRequest, CustomerSummary, NEW_CASE_SOURCES, OpsApi, SharedApi, errorMessage, humanize } from '@poc/api-client';

/** New case form: pick a customer, write any complaint, choose a source. */
@Component({
  selector: 'ops-new-case',
  imports: [FormsModule],
  template: `
    <h1>New case</h1>
    <form class="panel" (ngSubmit)="submit()" #f="ngForm">
      <label for="customer">Customer</label>
      <select id="customer" name="customer" [(ngModel)]="customerId" required>
        <option value="">Choose a customer...</option>
        @for (c of customers(); track c.id) {
          <option [value]="c.id">{{ c.name }}{{ c.simulated ? ' (simulated)' : '' }}</option>
        }
      </select>
      <label for="source">Source</label>
      <select id="source" name="source" [(ngModel)]="source">
        @for (s of sources; track s) {
          <option [value]="s">{{ label(s) }}</option>
        }
      </select>
      <label for="complaint">Complaint</label>
      <textarea id="complaint" name="complaint" rows="5" [(ngModel)]="complaint" maxlength="8000"
                placeholder="What the customer said"></textarea>
      @if (error()) {
        <p class="error">{{ error() }}</p>
      }
      <p>
        <button type="submit" class="primary" [disabled]="saving() || !valid()">Create case</button>
        <span class="muted"> The case is investigated automatically.</span>
      </p>
    </form>
  `,
  styles: `select, textarea { width: 100%; max-width: 640px; }`,
})
export class NewCasePage implements OnInit {
  private readonly ops = inject(OpsApi);
  private readonly shared = inject(SharedApi);
  private readonly router = inject(Router);

  readonly customers = signal<CustomerSummary[]>([]);
  readonly saving = signal(false);
  readonly error = signal<string | null>(null);
  readonly sources = NEW_CASE_SOURCES;
  readonly label = humanize;
  customerId = '';
  source: CreateCaseRequest['source'] = 'email';
  complaint = '';

  async ngOnInit(): Promise<void> {
    try {
      this.customers.set(await this.shared.listCustomers());
    } catch (err) {
      this.error.set(errorMessage(err));
    }
  }

  valid(): boolean {
    return !!this.customerId && !!this.complaint.trim();
  }

  async submit(): Promise<void> {
    if (!this.valid()) return;
    this.saving.set(true);
    this.error.set(null);
    try {
      const created = await this.ops.createCase({
        customer_id: this.customerId,
        source: this.source,
        complaint: this.complaint.trim(),
      });
      await this.router.navigate(['/cases', created.id]);
    } catch (err) {
      this.error.set(errorMessage(err));
    } finally {
      this.saving.set(false);
    }
  }
}
