import { DatePipe } from '@angular/common';
import { Component, effect, inject, input, signal, untracked } from '@angular/core';
import { RouterLink } from '@angular/router';
import { ConversationDetail, SharedApi, errorMessage, humanize } from '@poc/api-client';
import { Transcript } from '../components/transcript';

/** One conversation's transcript, for spot checks. */
@Component({
  selector: 'ops-conversation',
  imports: [DatePipe, RouterLink, Transcript],
  template: `
    <p><a routerLink="/conversations">&larr; Conversations</a></p>
    @if (detail(); as c) {
      <h1>{{ c.customer_name || c.customer_id }}</h1>
      <section class="panel">
        <dl class="kv">
          <dt>Started</dt><dd>{{ c.started_at | date: 'medium' }}</dd>
          <dt>Status</dt><dd>{{ c.status }}</dd>
          <dt>Outcome</dt><dd>{{ c.outcome ? label(c.outcome) : '-' }}</dd>
          <dt>Category</dt><dd>{{ c.category ? label(c.category) : '-' }}</dd>
          @if (c.case_id) { <dt>Case</dt><dd><a [routerLink]="['/cases', c.case_id]">Open case</a></dd> }
        </dl>
      </section>
      <section class="panel"><ops-transcript [messages]="c.messages" /></section>
    } @else if (error()) {
      <p class="error">{{ error() }}</p>
    } @else {
      <p class="muted">Loading...</p>
    }
  `,
})
export class ConversationPage {
  private readonly shared = inject(SharedApi);
  readonly id = input.required<string>();
  readonly detail = signal<ConversationDetail | null>(null);
  readonly error = signal<string | null>(null);
  readonly label = humanize;

  constructor() {
    effect(() => {
      const id = this.id();
      untracked(() => {
        this.detail.set(null);
        this.shared.getConversation(id).then(
          (c) => this.detail.set(c),
          (err) => this.error.set(errorMessage(err)),
        );
      });
    });
  }
}
