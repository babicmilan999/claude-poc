import { DatePipe, DecimalPipe, JsonPipe } from '@angular/common';
import { Component, OnDestroy, computed, effect, inject, input, output, signal, untracked } from '@angular/core';
import { FormsModule } from '@angular/forms';
import {
  EMPTY_TIMELINE,
  FeedbackRequest,
  ISSUE_TYPES,
  Investigation,
  IssueType,
  OpsApi,
  ROOT_CAUSES,
  RootCause,
  TimelineState,
  Verdict,
  applyInvestigationEvent,
  errorMessage,
  humanize,
  timelineFromEvents,
} from '@poc/api-client';
import { Subscription } from 'rxjs';
import { EvidenceList } from './evidence-list';

/**
 * One investigation: its timeline (live over SSE while queued or running, else the stored
 * events), the diagnosis or escalation with linked evidence, and the review panel.
 */
@Component({
  selector: 'ops-investigation-panel',
  imports: [DatePipe, DecimalPipe, JsonPipe, FormsModule, EvidenceList],
  templateUrl: './investigation-panel.html',
  styleUrl: './investigation-panel.scss',
})
export class InvestigationPanel implements OnDestroy {
  private readonly ops = inject(OpsApi);
  private stream?: Subscription;

  readonly investigationId = input.required<string>();
  /** Emitted when the investigation changed state (the case should be reloaded). */
  readonly changed = output<Investigation>();

  readonly investigation = signal<Investigation | null>(null);
  readonly timeline = signal<TimelineState>(EMPTY_TIMELINE);
  readonly live = signal(false);
  /** The live timeline gave up reconnecting; `reconnect()` resumes after the last event. */
  readonly streamLost = signal(false);
  readonly error = signal<string | null>(null);
  readonly highlighted = signal<string | null>(null);
  readonly submitting = signal(false);

  readonly label = humanize;
  readonly issueTypes = ISSUE_TYPES;
  readonly rootCauses = ROOT_CAUSES;

  // Review form
  verdict: Verdict = 'accepted';
  correctedIssueType: IssueType | '' = '';
  correctedRootCause: RootCause | '' = '';
  hint = '';
  notes = '';

  /** Diagnosis/escalation to show: live state while streaming, else the stored record. */
  readonly diagnosis = computed(() => this.timeline().diagnosis ?? this.investigation()?.diagnosis ?? null);
  readonly escalation = computed(() => this.timeline().escalation ?? this.investigation()?.escalation ?? null);
  readonly evidence = computed(() => {
    const fromTimeline = this.timeline().evidence;
    return fromTimeline.length ? fromTimeline : (this.investigation()?.evidence ?? []);
  });
  readonly canReview = computed(() => this.investigation()?.status === 'awaiting_review' && !this.live());

  constructor() {
    effect(() => {
      const id = this.investigationId();
      untracked(() => this.load(id));
    });
  }

  ngOnDestroy(): void {
    this.stream?.unsubscribe();
  }

  private async load(id: string): Promise<void> {
    this.stream?.unsubscribe();
    this.live.set(false);
    this.streamLost.set(false);
    this.error.set(null);
    this.timeline.set(EMPTY_TIMELINE);
    this.investigation.set(null);
    this.resetForm();
    try {
      const inv = await this.ops.getInvestigation(id);
      if (id !== this.investigationId()) return;
      this.investigation.set(inv);
      if (inv.status === 'queued' || inv.status === 'running') {
        this.follow(id);
      } else {
        const events = await this.ops.getInvestigationEvents(id);
        if (id === this.investigationId()) this.timeline.set(timelineFromEvents(events));
      }
    } catch (err) {
      this.error.set(errorMessage(err));
    }
  }

  /** Replays stored events after the last one seen, then streams live until the run stops. */
  private follow(id: string): void {
    this.stream?.unsubscribe();
    this.live.set(true);
    this.streamLost.set(false);
    const after = this.timeline().lastEventId ?? undefined;
    this.stream = this.ops.streamInvestigationEvents(id, after).subscribe({
      next: (event) => this.timeline.update((s) => applyInvestigationEvent(s, event)),
      error: (err) => {
        this.live.set(false);
        this.streamLost.set(true);
        this.error.set(`Live timeline stopped: ${errorMessage(err)}`);
      },
      complete: async () => {
        this.live.set(false);
        await this.refresh();
      },
    });
  }

  reconnect(): void {
    this.error.set(null);
    this.follow(this.investigationId());
  }

  private async refresh(): Promise<void> {
    try {
      const inv = await this.ops.getInvestigation(this.investigationId());
      this.investigation.set(inv);
      this.changed.emit(inv);
    } catch (err) {
      this.error.set(errorMessage(err));
    }
  }

  showEvidence(id: string): void {
    this.highlighted.set(id);
    globalThis.document?.getElementById(`inv-ev-${id}`)?.scrollIntoView?.({ behavior: 'smooth', block: 'center' });
  }

  formValid(): boolean {
    if (this.verdict === 'corrected') return !!this.correctedRootCause;
    if (this.verdict === 'rejected') return !!this.hint.trim();
    return true;
  }

  async submitReview(): Promise<void> {
    const inv = this.investigation();
    if (!inv || !this.formValid()) return;
    const feedback: FeedbackRequest = { verdict: this.verdict };
    if (this.verdict === 'corrected') {
      feedback.corrected_root_cause = this.correctedRootCause as RootCause;
      if (this.correctedIssueType) feedback.corrected_issue_type = this.correctedIssueType;
    }
    if (this.verdict === 'rejected') feedback.hint = this.hint.trim();
    if (this.notes.trim()) feedback.notes = this.notes.trim();
    this.submitting.set(true);
    this.error.set(null);
    try {
      const updated = await this.ops.submitFeedback(inv.id, feedback);
      this.investigation.set(updated);
      this.resetForm();
      if (updated.status === 'running' || updated.status === 'queued') {
        this.follow(inv.id); // rejected: the same run resumes with the hint
      } else {
        const events = await this.ops.getInvestigationEvents(inv.id, this.timeline().lastEventId ?? undefined);
        this.timeline.update((s) => events.reduce(applyInvestigationEvent, s));
      }
      this.changed.emit(updated);
    } catch (err) {
      this.error.set(errorMessage(err));
    } finally {
      this.submitting.set(false);
    }
  }

  private resetForm(): void {
    this.verdict = 'accepted';
    this.correctedIssueType = '';
    this.correctedRootCause = '';
    this.hint = '';
    this.notes = '';
  }
}
