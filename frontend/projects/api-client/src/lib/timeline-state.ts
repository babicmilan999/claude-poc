import {
  Diagnosis,
  Escalation,
  Evidence,
  InvestigationEvent,
  InvestigationStatus,
  Review,
} from './models';

/** A tool call, joined with the evidence it produced (matched by `call_id`). */
export interface ToolEntry {
  kind: 'tool';
  id: number;
  created_at: string;
  call_id: string;
  tool: string;
  args: Record<string, unknown>;
  evidence: Evidence | null;
}

/** Any other timeline event, shown as it came. Tokens are folded into `explanation`. */
export interface EventEntry {
  kind: 'event';
  id: number;
  created_at: string;
  event: Exclude<InvestigationEvent, { type: 'token' | 'tool_call' | 'evidence' }>;
}

export type TimelineEntry = ToolEntry | EventEntry;

export interface TimelineState {
  entries: TimelineEntry[];
  /** The final explanation as it streams (token events), reset when `diagnose` starts. */
  explanation: string;
  diagnosis: Diagnosis | null;
  escalation: Escalation | null;
  review: Review | null;
  status: InvestigationStatus | null;
  awaitingReview: boolean;
  evidence: Evidence[];
  /** Highest event id applied; the `Last-Event-ID` to resume from. */
  lastEventId: number | null;
}

export const EMPTY_TIMELINE: TimelineState = {
  entries: [],
  explanation: '',
  diagnosis: null,
  escalation: null,
  review: null,
  status: null,
  awaitingReview: false,
  evidence: [],
  lastEventId: null,
};

/** Applies one stored or live event. Events at or below `lastEventId` are ignored (replays). */
export function applyInvestigationEvent(state: TimelineState, event: InvestigationEvent): TimelineState {
  if (state.lastEventId !== null && event.id <= state.lastEventId) return state;
  const next: TimelineState = { ...state, lastEventId: event.id };
  const base = { id: event.id, created_at: event.created_at };

  switch (event.type) {
    case 'token':
      next.explanation = state.explanation + event.payload.text;
      return next;
    case 'tool_call':
      next.entries = [
        ...state.entries,
        { kind: 'tool', ...base, call_id: event.payload.call_id, tool: event.payload.tool, args: event.payload.args, evidence: null },
      ];
      return next;
    case 'evidence': {
      const ev = event.payload.evidence;
      next.evidence = [...state.evidence.filter((e) => e.id !== ev.id), ev];
      const index = state.entries.findIndex((e) => e.kind === 'tool' && e.call_id === event.payload.call_id);
      if (index === -1) {
        next.entries = [
          ...state.entries,
          { kind: 'tool', ...base, call_id: event.payload.call_id, tool: ev.tool, args: ev.args ?? {}, evidence: ev },
        ];
      } else {
        next.entries = state.entries.map((e, i) => (i === index ? { ...(e as ToolEntry), evidence: ev } : e));
      }
      return next;
    }
    default:
      break;
  }

  next.entries = [...state.entries, { kind: 'event', ...base, event }];
  switch (event.type) {
    case 'step_started':
      if (event.payload.node === 'diagnose') next.explanation = '';
      if (state.status === null || state.status === 'queued') next.status = 'running';
      next.awaitingReview = false;
      break;
    case 'diagnosis':
      next.diagnosis = event.payload.diagnosis;
      next.escalation = null;
      if (!next.explanation) next.explanation = event.payload.diagnosis.explanation;
      break;
    case 'escalated':
      next.escalation = event.payload.escalation;
      next.diagnosis = null;
      break;
    case 'awaiting_review':
      next.awaitingReview = true;
      next.status = 'awaiting_review';
      break;
    case 'review_recorded':
      next.review = event.payload.review;
      next.awaitingReview = false;
      if (event.payload.review.verdict === 'rejected') {
        // The same run resumes with the hint; a new diagnosis will follow.
        next.diagnosis = null;
        next.escalation = null;
        next.explanation = '';
      }
      break;
    case 'status_changed':
      next.status = event.payload.status;
      if (event.payload.status !== 'awaiting_review') next.awaitingReview = false;
      break;
  }
  return next;
}

export function timelineFromEvents(events: InvestigationEvent[]): TimelineState {
  return [...events].sort((a, b) => a.id - b.id).reduce(applyInvestigationEvent, EMPTY_TIMELINE);
}
