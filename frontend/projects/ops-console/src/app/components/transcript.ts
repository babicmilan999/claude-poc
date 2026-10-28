import { DatePipe } from '@angular/common';
import { Component, input } from '@angular/core';
import { Message } from '@poc/api-client';

/** A conversation transcript, read-only. */
@Component({
  selector: 'ops-transcript',
  imports: [DatePipe],
  template: `
    @for (m of messages(); track m.id) {
      <div class="line" [class]="m.role">
        <span class="who">{{ m.role }}</span>
        <span class="time muted">{{ m.created_at | date: 'HH:mm:ss' }}</span>
        <div class="content">{{ m.content }}</div>
        @if (m.sources.length) {
          <div class="muted small">Sources: @for (s of m.sources; track $index) {<span class="src">{{ s.title }}</span>}</div>
        }
        @if (m.rating) {
          <div class="muted small">Rated {{ m.rating === 'up' ? 'helpful' : 'not helpful' }}</div>
        }
      </div>
    } @empty {
      <p class="muted">No messages.</p>
    }
  `,
  styles: `
    .line { padding: 8px 0; border-bottom: 1px solid #eef0f4; }
    .who { font-weight: 600; text-transform: capitalize; margin-right: 8px; }
    .assistant .who { color: #2457c5; }
    .content { white-space: pre-wrap; margin-top: 2px; }
    .small { font-size: 12px; margin-top: 2px; }
    .src + .src::before { content: ', '; }
  `,
})
export class Transcript {
  readonly messages = input.required<Message[]>();
}
