import { Component, OnDestroy, effect, inject, input, signal, untracked } from '@angular/core';
import { RouterLink } from '@angular/router';
import { ChatState, EMPTY_CHAT, SharedApi, applyChatEvent, errorMessage, humanize } from '@poc/api-client';
import { Subscription } from 'rxjs';
import { MessageList } from '../components/message-list';

/** Follows one conversation live: stored messages first, then tokens as they stream. */
@Component({
  selector: 'ca-watch',
  imports: [MessageList, RouterLink],
  template: `
    <div class="head">
      <a routerLink="/watch">&larr; All</a>
      <strong>{{ customerName() }}</strong>
      @if (live()) {
        <span class="badge open">Live</span>
      } @else if (state().status === 'closed') {
        <span class="badge" [class]="state().outcome">{{ label(state().outcome) || 'Closed' }}</span>
      }
    </div>
    <ca-message-list [messages]="state().messages" emptyText="Waiting for the customer..." />
    @if (state().handoff; as handoff) {
      <p class="handoff" role="status">Handed off to the ops team. {{ handoff.notice }}</p>
    }
    @if (error()) {
      <p class="error">{{ error() }}</p>
    }
  `,
  styles: `
    .head { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; }
    .head .badge { margin-left: auto; }
    .handoff { background: #fff6e0; border-radius: 10px; padding: 10px 12px; }
  `,
})
export class WatchPage implements OnDestroy {
  private readonly shared = inject(SharedApi);
  private stream?: Subscription;
  private reopenTimer?: ReturnType<typeof setTimeout>;

  /** Route parameter `:id`. */
  readonly id = input.required<string>();
  readonly state = signal<ChatState>(EMPTY_CHAT);
  readonly customerName = signal('');
  readonly live = signal(false);
  readonly error = signal<string | null>(null);
  readonly label = humanize;

  constructor() {
    effect(() => {
      const id = this.id();
      untracked(() => this.follow(id));
    });
  }

  private follow(id: string, reopen = false): void {
    this.stream?.unsubscribe();
    if (!reopen) this.state.set(EMPTY_CHAT);
    this.error.set(null);
    this.shared.getConversation(id).then(
      (c) => this.customerName.set(c.customer_name ?? ''),
      (err) => this.error.set(errorMessage(err)),
    );
    this.live.set(true);
    this.stream = this.shared.conversationEvents(id).subscribe({
      next: (event) => this.state.update((s) => applyChatEvent(s, event)),
      error: (err) => {
        this.live.set(false);
        this.error.set(errorMessage(err));
      },
      complete: () => {
        this.live.set(false);
        // Closed while the conversation is still open (e.g. an idle timeout): reopen. The
        // replay repeats stored messages, which the chat state matches by id.
        if (this.state().status === 'open' && this.id() === id) {
          this.reopenTimer = setTimeout(() => this.follow(id, true), 1000);
        }
      },
    });
  }

  ngOnDestroy(): void {
    clearTimeout(this.reopenTimer);
    this.stream?.unsubscribe();
  }
}
