import { Component, OnDestroy, OnInit, computed, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import {
  ApiError,
  ChatState,
  CustomerApi,
  EMPTY_CHAT,
  SharedApi,
  applyChatEvent,
  chatStateFromDetail,
  endChatStream,
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
      const conversationId = id;
      this.stream = this.customer.sendMessage(conversationId, text).subscribe({
        next: (event) => this.state.update((s) => applyChatEvent(s, event)),
        error: (err) => void this.streamEnded(conversationId, err),
        complete: () => void this.streamEnded(conversationId),
      });
    } catch (err) {
      this.state.update((s) => ({ ...s, error: errorMessage(err) }));
      this.sending.set(false);
    }
  }

  /**
   * The answer stream is not resumable. If it ended before the answer completed (dropped
   * connection, server error) or the server refused the message, reload what it stored.
   */
  private async streamEnded(conversationId: string, err?: unknown): Promise<void> {
    const { state, incomplete } = endChatStream(this.state());
    this.state.set(err === undefined ? state : { ...state, error: errorMessage(err) });
    this.sending.set(false);
    if (err instanceof ApiError && err.status === 401) return; // logged out
    if (err instanceof ApiError && err.status === 404) {
      // The conversation is gone (e.g. the database was reset): start a new one.
      this.session.setConversation(null);
      this.state.set({ ...EMPTY_CHAT, error: 'That conversation no longer exists. Send your message again to start a new one.' });
      return;
    }
    // A clean end after a complete answer needs nothing; anything else (409: the
    // conversation was closed meanwhile; a drop mid-answer) reloads the stored state.
    if (err === undefined && !incomplete) return;
    if (this.session.conversationId() !== conversationId) return;
    try {
      const stored = chatStateFromDetail(await this.shared.getConversation(conversationId));
      if (this.session.conversationId() !== conversationId) return;
      const current = this.state();
      this.state.set({ ...stored, handoff: current.handoff ?? stored.handoff, error: current.error });
    } catch {
      // keep what we have
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
