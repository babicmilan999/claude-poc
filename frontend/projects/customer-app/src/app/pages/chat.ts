import { Component, OnDestroy, OnInit, computed, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import {
  ChatState,
  CustomerApi,
  EMPTY_CHAT,
  SharedApi,
  applyChatEvent,
  chatStateFromDetail,
  errorMessage,
} from '@poc/api-client';
import { Subscription } from 'rxjs';
import { MessageList, RateEvent } from '../components/message-list';
import { CustomerSession } from '../session';

/** The chat with the assistant: streamed answers, sources, 👍 / 👎, handoff notice. */
@Component({
  selector: 'ca-chat',
  imports: [FormsModule, MessageList],
  template: `
    <div class="chat-head">
      <strong>Help</strong>
      @if (state().outcome === 'resolved') {
        <span class="badge resolved">Resolved</span>
      }
      <button type="button" class="link" (click)="newConversation()" [disabled]="sending()">New conversation</button>
    </div>
    <ca-message-list
      class="messages"
      [messages]="state().messages"
      [rateable]="state().status === 'open'"
      emptyText="Ask about a card payment, a scheduled payment or a direct debit."
      (rate)="rate($event)"
    />
    @if (state().handoff; as handoff) {
      <p class="handoff" role="status">{{ handoff.notice || 'An agent will look into this and get back to you.' }}</p>
    }
    @if (state().error) {
      <p class="error">{{ state().error }}</p>
    }
    @if (closed()) {
      <p class="muted closed">This conversation is closed.
        <button type="button" class="link" (click)="newConversation()">Start a new one</button></p>
    } @else {
      <form class="composer" (ngSubmit)="send()">
        <input name="draft" [(ngModel)]="draft" placeholder="Type your question" autocomplete="off" maxlength="4000"
               [disabled]="sending()" aria-label="Message" />
        <button type="submit" class="primary" [disabled]="sending() || !draft.trim()">Send</button>
      </form>
    }
  `,
  styles: `
    :host { display: flex; flex-direction: column; flex: 1; min-height: 0; }
    .chat-head { display: flex; align-items: center; gap: 8px; margin-bottom: 12px; }
    .chat-head .link { margin-left: auto; }
    .messages { flex: 1; }
    .handoff { background: #fff6e0; border-radius: 10px; padding: 10px 12px; }
    .composer { display: flex; gap: 8px; margin-top: 12px; position: sticky; bottom: 0; }
    .composer input { flex: 1; }
    .closed { text-align: center; }
  `,
})
export class ChatPage implements OnInit, OnDestroy {
  private readonly customer = inject(CustomerApi);
  private readonly shared = inject(SharedApi);
  private readonly session = inject(CustomerSession);
  private stream?: Subscription;

  readonly state = signal<ChatState>(EMPTY_CHAT);
  readonly sending = signal(false);
  readonly closed = computed(() => this.state().status === 'closed');
  draft = '';

  async ngOnInit(): Promise<void> {
    const id = this.session.conversationId();
    if (!id) return;
    try {
      this.state.set(chatStateFromDetail(await this.shared.getConversation(id)));
    } catch {
      this.session.setConversation(null);
    }
  }

  ngOnDestroy(): void {
    this.stream?.unsubscribe();
  }

  async send(): Promise<void> {
    const text = this.draft.trim();
    if (!text || this.sending()) return;
    this.sending.set(true);
    this.draft = '';
    try {
      let id = this.session.conversationId();
      if (!id) {
        id = (await this.customer.createConversation()).id;
        this.session.setConversation(id);
      }
      this.stream = this.customer.sendMessage(id, text).subscribe({
        next: (event) => this.state.update((s) => applyChatEvent(s, event)),
        error: (err) => {
          this.state.update((s) => ({ ...s, error: errorMessage(err) }));
          this.sending.set(false);
        },
        complete: () => this.sending.set(false),
      });
    } catch (err) {
      this.state.update((s) => ({ ...s, error: errorMessage(err) }));
      this.sending.set(false);
    }
  }

  async rate({ message, rating }: RateEvent): Promise<void> {
    const id = this.session.conversationId();
    if (!id) return;
    try {
      const updated = await this.customer.rateMessage(id, message.id, rating);
      this.state.update((s) => ({
        ...s,
        // 👍 resolves the conversation (it stays open); 👎 counts as "that didn't help".
        outcome: rating === 'up' ? 'resolved' : s.outcome === 'resolved' ? null : s.outcome,
        messages: s.messages.map((m) => (m.id === updated.id ? { ...m, rating: updated.rating } : m)),
      }));
    } catch (err) {
      this.state.update((s) => ({ ...s, error: errorMessage(err) }));
    }
  }

  newConversation(): void {
    this.stream?.unsubscribe();
    this.session.setConversation(null);
    this.state.set(EMPTY_CHAT);
    this.sending.set(false);
  }
}
