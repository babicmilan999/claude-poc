import { Component, OnInit, computed, inject, signal } from '@angular/core';
import { Router } from '@angular/router';
import { CustomerSummary, SharedApi, errorMessage } from '@poc/api-client';
import { CustomerSession } from '../session';

/** Dev-only "log in as": pick one of the seeded (or simulated) customers. */
@Component({
  selector: 'ca-login',
  template: `
    <h1>Log in as</h1>
    <p class="muted">Local development only: no real authentication.</p>
    @if (session.notice(); as notice) {
      <p class="notice" role="status">{{ notice }}</p>
    }
    @if (error()) {
      <p class="error">{{ error() }} <button type="button" class="link" (click)="load()">Try again</button></p>
    }
    @for (c of seeded(); track c.id) {
      <button type="button" class="list-button" (click)="logIn(c)">{{ c.name }}</button>
    }
    @if (simulated().length) {
      <h2 class="muted">Simulated customers</h2>
      @for (c of simulated(); track c.id) {
        <button type="button" class="list-button" (click)="logIn(c)">{{ c.name }}</button>
      }
    }
    @if (loading()) {
      <p class="muted">Loading customers...</p>
    }
  `,
  styles: `h1 { font-size: 22px; margin: 0 0 4px; } h2 { font-size: 14px; margin: 16px 0 8px; }
    .notice { background: #fff6e0; border-radius: 10px; padding: 10px 12px; }`,
})
export class LoginPage implements OnInit {
  private readonly shared = inject(SharedApi);
  protected readonly session = inject(CustomerSession);
  private readonly router = inject(Router);

  readonly customers = signal<CustomerSummary[]>([]);
  readonly loading = signal(true);
  readonly error = signal<string | null>(null);
  readonly seeded = computed(() => this.customers().filter((c) => !c.simulated));
  readonly simulated = computed(() => this.customers().filter((c) => c.simulated));

  ngOnInit(): Promise<void> {
    return this.load();
  }

  async load(): Promise<void> {
    this.loading.set(true);
    this.error.set(null);
    try {
      this.customers.set(await this.shared.listCustomers());
    } catch (err) {
      this.error.set(errorMessage(err));
    } finally {
      this.loading.set(false);
    }
  }

  logIn(customer: CustomerSummary): void {
    this.session.logIn(customer);
    this.router.navigateByUrl('/account');
  }
}
