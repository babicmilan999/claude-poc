import { DatePipe } from '@angular/common';
import { Component, OnInit, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { Router, RouterLink } from '@angular/router';
import { CONVERSATION_OUTCOMES, Conversation, ConversationOutcome, SharedApi, errorMessage, humanize } from '@poc/api-client';

/** Assistant conversations with their outcome, for spot checks. */
@Component({
  selector: 'ops-conversations',
  imports: [DatePipe, FormsModule, RouterLink],
  template: `
    <h1>Conversations</h1>
    <div class="toolbar">
      <label class="inline" for="outcome">Outcome</label>
      <select id="outcome" name="outcome" [(ngModel)]="outcome" (ngModelChange)="load()">
        <option value="">All</option>
        @for (o of outcomes; track o) { <option [value]="o">{{ label(o) }}</option> }
      </select>
      <label class="inline" for="simulated">Customers</label>
      <select id="simulated" name="simulated" [(ngModel)]="simulated" (ngModelChange)="load()">
        <option value="">All</option>
        <option value="false">Real</option>
        <option value="true">Simulated</option>
      </select>
      <span class="spacer"></span>
      <span class="muted">{{ total() }} conversations</span>
    </div>
    @if (error()) { <p class="error">{{ error() }}</p> }
    <table class="grid">
      <thead><tr><th>Customer</th><th>Started</th><th>Category</th><th>Status</th><th>Outcome</th><th>Rating</th><th>Case</th></tr></thead>
      <tbody>
        @for (c of conversations(); track c.id) {
          <tr class="clickable" (click)="open(c)">
            <td>{{ c.customer_name || c.customer_id }} @if (c.simulated) { <span class="badge sim">simulated</span> }</td>
            <td>{{ c.started_at | date: 'medium' }}</td>
            <td>{{ c.category ? label(c.category) : '-' }}</td>
            <td>{{ c.status }}</td>
            <td><span class="badge outcome" [class]="c.outcome">{{ c.outcome ? label(c.outcome) : '-' }}</span></td>
            <td>{{ c.rating === 'up' ? 'helpful' : c.rating === 'down' ? 'not helpful' : '-' }}</td>
            <td>
              @if (c.case_id) {
                <a [routerLink]="['/cases', c.case_id]" (click)="$event.stopPropagation()">Case</a>
              } @else { - }
            </td>
          </tr>
        } @empty {
          <tr><td colspan="7" class="muted">No conversations.</td></tr>
        }
      </tbody>
    </table>
  `,
  styles: `.inline { display: inline-flex; margin: 0; font-weight: normal; }`,
})
export class ConversationsPage implements OnInit {
  private readonly shared = inject(SharedApi);
  private readonly router = inject(Router);
  readonly conversations = signal<Conversation[]>([]);
  readonly total = signal(0);
  readonly error = signal<string | null>(null);
  readonly outcomes = CONVERSATION_OUTCOMES;
  readonly label = humanize;
  outcome: ConversationOutcome | '' = '';
  simulated: '' | 'true' | 'false' = '';

  ngOnInit(): void {
    this.load();
  }

  async load(): Promise<void> {
    try {
      const page = await this.shared.listConversations({
        outcome: this.outcome || undefined,
        simulated: this.simulated === '' ? undefined : this.simulated === 'true',
        limit: 200,
      });
      this.conversations.set(page.items);
      this.total.set(page.total);
      this.error.set(null);
    } catch (err) {
      this.error.set(errorMessage(err));
    }
  }

  open(c: Conversation): void {
    this.router.navigate(['/conversations', c.id]);
  }
}
