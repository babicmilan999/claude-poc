import { DatePipe } from '@angular/common';
import { Component, OnInit, inject, signal } from '@angular/core';
import { RouterLink } from '@angular/router';
import { Conversation, SharedApi, errorMessage, humanize } from '@poc/api-client';

/** Watch mode: pick a simulated customer's conversation to follow live. */
@Component({
  selector: 'ca-watch-list',
  imports: [DatePipe, RouterLink],
  template: `
    <div class="head">
      <h1>Watch mode</h1>
      <button type="button" (click)="load()">Refresh</button>
    </div>
    <p class="muted">Simulated customers chatting with the assistant. Start one with "Simulate customer" in the ops console.</p>
    @if (error()) {
      <p class="error">{{ error() }}</p>
    }
    @for (c of conversations(); track c.id) {
      <a class="card item" [routerLink]="['/watch', c.id]">
        <span>
          <strong>{{ c.customer_name || 'Customer' }}</strong>
          <span class="muted"> {{ c.started_at | date: 'HH:mm' }}</span>
          @if (c.category) {
            <br /><span class="muted">{{ label(c.category) }}</span>
          }
        </span>
        @if (c.status === 'open') {
          <span class="badge open">Live</span>
        } @else {
          <span class="badge" [class]="c.outcome">{{ label(c.outcome) || 'closed' }}</span>
        }
      </a>
    } @empty {
      <p class="muted">No simulated conversations yet.</p>
    }
  `,
  styles: `
    .head { display: flex; justify-content: space-between; align-items: center; }
    h1 { font-size: 22px; margin: 0; }
    .item { display: flex; justify-content: space-between; align-items: center; text-decoration: none; color: inherit; }
  `,
})
export class WatchListPage implements OnInit {
  private readonly shared = inject(SharedApi);
  readonly conversations = signal<Conversation[]>([]);
  readonly error = signal<string | null>(null);
  readonly label = humanize;

  ngOnInit(): void {
    this.load();
  }

  async load(): Promise<void> {
    try {
      const page = await this.shared.listConversations({ simulated: true, limit: 30 });
      this.conversations.set(page.items);
      this.error.set(null);
    } catch (err) {
      this.error.set(errorMessage(err));
    }
  }
}
