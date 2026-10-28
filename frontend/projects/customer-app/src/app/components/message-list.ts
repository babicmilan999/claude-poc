import { Component, input, output } from '@angular/core';
import { ChatMessageView, Rating } from '@poc/api-client';

export interface RateEvent {
  message: ChatMessageView;
  rating: Rating;
}

/** The chat transcript: bubbles, streamed text, source links and the 👍 / 👎 buttons. */
@Component({
  selector: 'ca-message-list',
  template: `
    @for (m of messages(); track m.id) {
      <div class="msg" [class]="m.role" [attr.data-message-id]="m.id">
        <div class="bubble">
          {{ m.content }}@if (m.streaming) {<span class="caret" aria-label="typing">&#9612;</span>}
        </div>
        @if (m.sources.length) {
          <div class="sources">
            @for (s of m.sources; track $index) {
              @if (s.kind === 'article' && s.url) {
                <a class="source" [href]="s.url" target="_blank" rel="noopener">{{ s.title }}</a>
              } @else {
                <span class="source data">{{ s.title }}</span>
              }
            }
          </div>
        }
        @if (m.role === 'assistant' && !m.streaming && m.sources.length) {
          <div class="rate">
            @if (rateable()) {
              <span class="muted">Did this help?</span>
              <button type="button" class="thumb" [class.on]="m.rating === 'up'" aria-label="Helpful"
                      (click)="rate.emit({ message: m, rating: 'up' })">&#128077;</button>
              <button type="button" class="thumb" [class.on]="m.rating === 'down'" aria-label="Not helpful"
                      (click)="rate.emit({ message: m, rating: 'down' })">&#128078;</button>
            } @else if (m.rating) {
              <span class="muted">Rated {{ m.rating === 'up' ? '&#128077;' : '&#128078;' }}</span>
            }
          </div>
        }
      </div>
    } @empty {
      <p class="muted empty">{{ emptyText() }}</p>
    }
  `,
  styles: `
    :host { display: flex; flex-direction: column; gap: 10px; }
    .msg { display: flex; flex-direction: column; max-width: 85%; }
    .msg.customer { align-self: flex-end; align-items: flex-end; }
    .msg.assistant, .msg.system { align-self: flex-start; }
    .bubble { padding: 9px 12px; border-radius: 14px; background: #fff; white-space: pre-wrap; line-height: 1.4; }
    .customer .bubble { background: #2457c5; color: #fff; }
    .system .bubble { background: #fff6e0; }
    .caret { animation: blink 1s steps(2) infinite; }
    @keyframes blink { 50% { opacity: 0; } }
    .sources { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; }
    .source { font-size: 12px; padding: 2px 8px; border-radius: 10px; background: #e7eefc; text-decoration: none; }
    .source.data { background: #eef1f6; color: #3a4357; }
    .rate { display: flex; align-items: center; gap: 6px; margin-top: 4px; font-size: 13px; }
    .thumb { padding: 2px 8px; }
    .thumb.on { background: #e7eefc; border-color: #2457c5; }
    .empty { text-align: center; margin-top: 40px; }
  `,
})
export class MessageList {
  readonly messages = input.required<ChatMessageView[]>();
  readonly rateable = input(false);
  readonly emptyText = input('No messages yet.');
  readonly rate = output<RateEvent>();
}
