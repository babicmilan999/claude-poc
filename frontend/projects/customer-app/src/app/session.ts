import { Injectable, signal } from '@angular/core';
import { CustomerSummary } from '@poc/api-client';

const KEY = 'customer-app.session';

interface Stored {
  customer: CustomerSummary | null;
  conversationId: string | null;
}

/**
 * The dev-only "log in as" session: which customer the app acts as (sent as
 * `X-Customer-Id`) and the conversation in progress. Kept in sessionStorage per tab.
 */
@Injectable({ providedIn: 'root' })
export class CustomerSession {
  private readonly stored = this.restore();
  readonly customer = signal<CustomerSummary | null>(this.stored.customer);
  readonly conversationId = signal<string | null>(this.stored.conversationId);
  /** Why the app logged out on its own (shown on "log in as"); not stored. */
  readonly notice = signal<string | null>(null);

  logIn(customer: CustomerSummary): void {
    this.notice.set(null);
    this.customer.set(customer);
    this.conversationId.set(null);
    this.save();
  }

  logOut(notice: string | null = null): void {
    this.notice.set(notice);
    this.customer.set(null);
    this.conversationId.set(null);
    this.save();
  }

  setConversation(id: string | null): void {
    this.conversationId.set(id);
    this.save();
  }

  private restore(): Stored {
    try {
      const raw = globalThis.sessionStorage?.getItem(KEY);
      if (raw) return JSON.parse(raw) as Stored;
    } catch {
      // unavailable or corrupt: start logged out
    }
    return { customer: null, conversationId: null };
  }

  private save(): void {
    try {
      globalThis.sessionStorage?.setItem(
        KEY,
        JSON.stringify({ customer: this.customer(), conversationId: this.conversationId() } satisfies Stored),
      );
    } catch {
      // ignore
    }
  }
}
