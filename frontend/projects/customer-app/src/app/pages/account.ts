import { CurrencyPipe, DatePipe } from '@angular/common';
import { Component, OnInit, inject, signal } from '@angular/core';
import { RouterLink } from '@angular/router';
import { CustomerApi, CustomerOverview, errorMessage } from '@poc/api-client';

/** Minimal account view: accounts, cards with their status, recent transactions. */
@Component({
  selector: 'ca-account',
  imports: [CurrencyPipe, DatePipe, RouterLink],
  template: `
    @if (error()) {
      <p class="error">{{ error() }}</p>
    }
    @if (overview(); as o) {
      <section class="card">
        <h2>Accounts</h2>
        @for (a of o.accounts; track a.id) {
          <div class="row">
            <span>{{ a.name }} <span class="muted">{{ a.masked_account_number }}</span></span>
            <strong class="balance">{{ +a.balance | currency: a.currency }}</strong>
          </div>
        }
      </section>
      <section class="card">
        <h2>Cards</h2>
        @for (c of o.cards; track c.id) {
          <div class="row card-row">
            <span>Card ending {{ c.last4 }} <span class="muted">exp {{ c.expiry | date: 'MM/yy' }}</span></span>
            <span class="badge" [class]="c.status">{{ c.status }}</span>
          </div>
        }
      </section>
      <section class="card">
        <h2>Recent transactions</h2>
        @for (t of o.recent_transactions; track t.id) {
          <div class="row">
            <span>{{ t.description }} <span class="muted">{{ t.booked_at | date: 'd MMM' }}</span></span>
            <span [class.credit]="+t.amount > 0">{{ +t.amount | currency: t.currency }}</span>
          </div>
        } @empty {
          <p class="muted">No recent transactions.</p>
        }
      </section>
      <a routerLink="/chat" class="help">Something wrong? Ask for help</a>
    } @else if (!error()) {
      <p class="muted">Loading...</p>
    }
  `,
  styles: `.credit { color: #1d6b35; } .help { text-align: center; display: block; margin-top: 4px; }`,
})
export class AccountPage implements OnInit {
  private readonly customer = inject(CustomerApi);
  readonly overview = signal<CustomerOverview | null>(null);
  readonly error = signal<string | null>(null);

  async ngOnInit(): Promise<void> {
    try {
      this.overview.set(await this.customer.getMyOverview());
    } catch (err) {
      this.error.set(errorMessage(err));
    }
  }
}
