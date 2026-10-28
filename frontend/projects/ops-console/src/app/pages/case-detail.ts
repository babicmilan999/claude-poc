import { DatePipe, JsonPipe } from '@angular/common';
import { Component, computed, effect, inject, input, signal, untracked } from '@angular/core';
import { RouterLink } from '@angular/router';
import { CaseDetail, Investigation, OpsApi, errorMessage, humanize } from '@poc/api-client';
import { EvidenceList } from '../components/evidence-list';
import { InvestigationPanel } from '../components/investigation-panel';
import { Transcript } from '../components/transcript';

/** Case detail: summary, handoff transcript and evidence, investigations and review. */
@Component({
  selector: 'ops-case-detail',
  imports: [DatePipe, JsonPipe, RouterLink, EvidenceList, InvestigationPanel, Transcript],
  template: `
    <p><a routerLink="/cases">&larr; Case queue</a></p>
    @if (detail(); as c) {
      <div class="toolbar">
        <h1 class="title">{{ c.customer_name || c.customer_id }}</h1>
        <span class="badge case-status" [class]="c.status">{{ label(c.status) }}</span>
        @if (c.simulated) { <span class="badge sim">simulated</span> }
        <span class="spacer"></span>
        @if (canInvestigate()) {
          <button type="button" class="investigate" (click)="investigate()" [disabled]="busy()">Investigate</button>
        }
        <button type="button" class="reinvestigate" (click)="reinvestigate()" [disabled]="busy() || !canReinvestigate()">Re-investigate</button>
      </div>
      @if (error()) {
        <p class="error">{{ error() }}</p>
      }

      <section class="panel">
        <dl class="kv">
          <dt>Source</dt><dd>{{ label(c.source) }}</dd>
          <dt>Category</dt><dd>{{ c.category ? label(c.category) : 'Not triaged yet' }}</dd>
          <dt>Created</dt><dd>{{ c.created_at | date: 'medium' }}</dd>
          @if (c.complaint) { <dt>Complaint</dt><dd class="complaint">{{ c.complaint }}</dd> }
          @if (c.summary) { <dt>Handoff summary</dt><dd>{{ c.summary }}</dd> }
          @if (c.conversation_id) {
            <dt>Conversation</dt><dd><a [routerLink]="['/conversations', c.conversation_id]">Open conversation</a></dd>
          }
        </dl>
        @if (c.trigger_payload) { <pre>{{ c.trigger_payload | json }}</pre> }
      </section>

      @if (c.transcript.length) {
        <section class="panel transcript">
          <h2>Conversation transcript</h2>
          <ops-transcript [messages]="c.transcript" />
          <h2 class="ev-head">Assistant's evidence</h2>
          <ops-evidence-list [evidence]="c.handoff_evidence" prefix="handoff-ev" />
        </section>
      }

      @if (selectedId(); as id) {
        <section class="panel">
          <ops-investigation-panel [investigationId]="id" (changed)="onChanged($event)" />
        </section>
      } @else {
        <section class="panel muted">No investigation yet.</section>
      }

      @if (c.investigations.length > 1) {
        <section class="panel">
          <h2>All investigations</h2>
          <table class="grid">
            <thead><tr><th>Queued</th><th>Trigger</th><th>Issue type</th><th>Status</th><th>Verdict</th></tr></thead>
            <tbody>
              @for (i of c.investigations; track i.id) {
                <tr class="clickable inv-row" [class.selected]="i.id === selectedId()" (click)="select(i.id)">
                  <td>{{ i.queued_at | date: 'medium' }}</td>
                  <td>{{ i.trigger }}</td>
                  <td>{{ i.issue_type ? label(i.issue_type) : '-' }}</td>
                  <td><span class="badge" [class]="i.status">{{ label(i.status) }}</span></td>
                  <td>{{ i.verdict ? label(i.verdict) : '-' }}</td>
                </tr>
              }
            </tbody>
          </table>
        </section>
      }
    } @else if (error()) {
      <p class="error">{{ error() }}</p>
    } @else {
      <p class="muted">Loading case...</p>
    }
  `,
  styles: `
    .title { margin: 0; }
    .complaint { white-space: pre-wrap; }
    .ev-head { margin-top: 16px; }
    tr.selected { background: #eef3ff; }
  `,
})
export class CaseDetailPage {
  private readonly ops = inject(OpsApi);

  /** Route parameter `:id`. */
  readonly id = input.required<string>();
  readonly detail = signal<CaseDetail | null>(null);
  readonly selectedId = signal<string | null>(null);
  readonly busy = signal(false);
  readonly error = signal<string | null>(null);
  readonly label = humanize;

  private readonly active = computed(() => {
    const c = this.detail();
    return c?.investigations.find((i) => i.id === c.active_investigation_id) ?? null;
  });
  readonly canInvestigate = computed(() => {
    const c = this.detail();
    return !!c && c.status !== 'resolved' && !c.active_investigation_id;
  });
  readonly canReinvestigate = computed(() => {
    const c = this.detail();
    const active = this.active();
    return !!c && c.status !== 'resolved' && (!active || active.status === 'awaiting_review');
  });

  constructor() {
    effect(() => {
      const id = this.id();
      untracked(() => {
        this.detail.set(null);
        this.selectedId.set(null);
        this.load(id);
      });
    });
  }

  async load(id = this.id()): Promise<void> {
    try {
      const detail = await this.ops.getCase(id);
      if (id !== this.id()) return;
      this.detail.set(detail);
      this.error.set(null);
      if (!this.selectedId() || !detail.investigations.some((i) => i.id === this.selectedId())) {
        this.selectedId.set(detail.active_investigation_id ?? detail.investigations[0]?.id ?? null);
      }
    } catch (err) {
      this.error.set(errorMessage(err));
    }
  }

  select(id: string): void {
    this.selectedId.set(id);
  }

  onChanged(_inv: Investigation): void {
    this.load();
  }

  async investigate(): Promise<void> {
    await this.start(() => this.ops.investigateCase(this.id()));
  }

  async reinvestigate(): Promise<void> {
    await this.start(() => this.ops.reinvestigateCase(this.id()));
  }

  private async start(call: () => Promise<Investigation>): Promise<void> {
    this.busy.set(true);
    this.error.set(null);
    try {
      const inv = await call();
      this.selectedId.set(inv.id);
      await this.load();
    } catch (err) {
      this.error.set(errorMessage(err));
    } finally {
      this.busy.set(false);
    }
  }
}
