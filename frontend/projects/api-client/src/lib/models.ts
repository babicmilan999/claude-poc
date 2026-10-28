// Types written by hand from contracts/agent-api.yaml (and the shared enums in
// contracts/scenario.schema.json). The contract is the source of truth: when it changes,
// update this file and the mock server; the contract tests validate the mock against it.

// --- shared enums (scenario.schema.json#/$defs) ---------------------------------------
export const ISSUE_TYPES = ['card_decline', 'missed_payment', 'failed_direct_debit', 'other'] as const;
export type IssueType = (typeof ISSUE_TYPES)[number];

export const ROOT_CAUSES = [
  'card_frozen_by_customer',
  'insufficient_funds',
  'card_expired',
  'contactless_limit_exceeded',
  'merchant_category_blocked',
  'fraud_rule_triggered',
  'daily_payment_limit_exceeded',
  'payee_account_closed',
  'mandate_cancelled_by_customer',
  'account_closed_by_originator',
  'online_payments_disabled',
  'card_lost_or_stolen',
  'account_not_active',
  'invalid_payee_details',
  'no_mandate',
  'payer_account_closed',
  'unknown',
] as const;
export type RootCause = (typeof ROOT_CAUSES)[number];

// --- common ----------------------------------------------------------------------------
export interface Problem {
  type?: string;
  title: string;
  status: number;
  detail?: string;
}

/** Decimal amount as a string, e.g. "12.50". */
export type Money = string;

export interface Page<T> {
  items: T[];
  total: number;
}

// --- customers -------------------------------------------------------------------------
export interface CustomerSummary {
  id: string;
  name: string;
  simulated?: boolean;
}

export interface OverviewAccount {
  id: string;
  name: string;
  masked_account_number: string;
  currency: string;
  balance: Money;
  status: 'active' | 'frozen' | 'closed';
}

export interface OverviewCard {
  id: string;
  last4: string;
  status: 'active' | 'frozen' | 'blocked' | 'expired';
  expiry: string;
}

export interface OverviewTransaction {
  id: string;
  amount: Money;
  currency: string;
  description: string;
  booked_at: string;
}

export interface CustomerOverview {
  customer: CustomerSummary;
  accounts: OverviewAccount[];
  cards: OverviewCard[];
  recent_transactions: OverviewTransaction[];
}

// --- conversations ---------------------------------------------------------------------
export const CONVERSATION_STATUSES = ['open', 'closed'] as const;
export type ConversationStatus = (typeof CONVERSATION_STATUSES)[number];
export const CONVERSATION_OUTCOMES = ['resolved', 'handed_off', 'abandoned'] as const;
export type ConversationOutcome = (typeof CONVERSATION_OUTCOMES)[number];
export type Rating = 'up' | 'down';

export interface Conversation {
  id: string;
  customer_id: string;
  customer_name?: string;
  status: ConversationStatus;
  outcome?: ConversationOutcome | null;
  category?: IssueType | null;
  rating?: Rating | null;
  simulated: boolean;
  case_id?: string | null;
  started_at: string;
  ended_at?: string | null;
}

export interface Source {
  kind: 'article' | 'account_data';
  title: string;
  article_id?: string;
  url?: string;
  tool?: string;
}

export interface Message {
  id: number;
  conversation_id: string;
  role: 'customer' | 'assistant' | 'system';
  content: string;
  sources: Source[];
  rating?: Rating | null;
  created_at: string;
}

export interface ConversationDetail extends Conversation {
  messages: Message[];
}

export type ConversationPage = Page<Conversation>;

export interface ConversationQuery {
  status?: ConversationStatus;
  outcome?: ConversationOutcome;
  simulated?: boolean;
  customer_id?: string;
  limit?: number;
  offset?: number;
}

interface ChatEventBase {
  id: string;
  conversation_id: string;
  created_at: string;
}
export interface ChatMessageCreatedEvent extends ChatEventBase {
  type: 'message_created';
  message: Message;
}
export interface ChatTokenEvent extends ChatEventBase {
  type: 'token';
  message_id: number;
  text: string;
}
export interface ChatSourcesEvent extends ChatEventBase {
  type: 'sources';
  message_id: number;
  sources: Source[];
}
export interface ChatMessageCompletedEvent extends ChatEventBase {
  type: 'message_completed';
  message: Message;
}
export interface ChatHandoffEvent extends ChatEventBase {
  type: 'handoff';
  case_id: string;
  notice: string;
}
export interface ChatConversationStatusEvent extends ChatEventBase {
  type: 'conversation_status';
  status: ConversationStatus;
  outcome?: ConversationOutcome | null;
}
export interface ChatErrorEvent extends ChatEventBase {
  type: 'error';
  message: string;
}
export type ChatEvent =
  | ChatMessageCreatedEvent
  | ChatTokenEvent
  | ChatSourcesEvent
  | ChatMessageCompletedEvent
  | ChatHandoffEvent
  | ChatConversationStatusEvent
  | ChatErrorEvent;

// --- cases -----------------------------------------------------------------------------
export const CASE_SOURCES = [
  'assistant_handoff',
  'email',
  'phone',
  'ops_manual',
  'simulated_email',
  'event',
] as const;
export type CaseSource = (typeof CASE_SOURCES)[number];
export const CASE_STATUSES = [
  'new',
  'investigating',
  'awaiting_review',
  'needs_manual',
  'escalated',
  'resolved',
] as const;
export type CaseStatus = (typeof CASE_STATUSES)[number];

export interface Case {
  id: string;
  customer_id: string;
  customer_name?: string;
  conversation_id?: string | null;
  source: CaseSource;
  category?: IssueType | null;
  complaint?: string | null;
  trigger_payload?: Record<string, unknown> | null;
  summary?: string | null;
  status: CaseStatus;
  simulated: boolean;
  active_investigation_id?: string | null;
  created_at: string;
  updated_at: string;
}

export interface CaseDetail extends Case {
  transcript: Message[];
  handoff_evidence: Evidence[];
  investigations: InvestigationSummary[];
}

export type CasePage = Page<Case>;

export interface CaseQuery {
  status?: CaseStatus[];
  source?: CaseSource;
  category?: IssueType;
  limit?: number;
  offset?: number;
}

export const NEW_CASE_SOURCES = ['email', 'phone', 'ops_manual'] as const;
export interface CreateCaseRequest {
  customer_id: string;
  source: (typeof NEW_CASE_SOURCES)[number];
  complaint?: string;
  trigger_payload?: Record<string, unknown>;
}

export interface CaseCreatedEvent {
  id: string;
  type: 'case_created';
  created_at: string;
  case: Case;
}
export interface CaseStatusChangedEvent {
  id: string;
  type: 'status_changed';
  created_at: string;
  case_id: string;
  previous_status?: CaseStatus;
  status: CaseStatus;
  category?: IssueType | null;
}
export type CaseEvent = CaseCreatedEvent | CaseStatusChangedEvent;

// --- investigations --------------------------------------------------------------------
export const INVESTIGATION_STATUSES = [
  'queued',
  'running',
  'awaiting_review',
  'completed',
  'failed',
  'superseded',
] as const;
export type InvestigationStatus = (typeof INVESTIGATION_STATUSES)[number];
/** Statuses after which the timeline stream closes. */
export const STOPPED_INVESTIGATION_STATUSES: readonly InvestigationStatus[] = [
  'awaiting_review',
  'completed',
  'failed',
  'superseded',
];
export type InvestigationTrigger = 'auto' | 'manual';
export type Verdict = 'accepted' | 'corrected' | 'rejected';

export interface InvestigationSummary {
  id: string;
  case_id: string;
  trigger: InvestigationTrigger;
  status: InvestigationStatus;
  issue_type?: IssueType | null;
  verdict?: Verdict | null;
  queued_at: string;
  started_at?: string | null;
  finished_at?: string | null;
}

export interface Evidence {
  id: string;
  origin: 'assistant' | 'investigation';
  tool: string;
  args?: Record<string, unknown>;
  summary: string;
  data?: unknown;
  created_at: string;
}

export interface Diagnosis {
  id: string;
  issue_type: IssueType;
  root_cause: RootCause;
  explanation: string;
  confidence: number;
  evidence_ids: string[];
  recommended_action: string;
}

export interface Escalation {
  summary: string;
  evidence_ids: string[];
}

export interface Review {
  verdict: Verdict;
  corrected_issue_type?: IssueType | null;
  corrected_root_cause?: RootCause | null;
  hint?: string | null;
  notes?: string | null;
  created_at: string;
}

export interface Investigation extends InvestigationSummary {
  attempts?: number;
  steps_used: number;
  category_changed?: boolean;
  evidence: Evidence[];
  diagnosis?: Diagnosis | null;
  escalation?: Escalation | null;
  review?: Review | null;
  error?: string | null;
}

export interface FeedbackRequest {
  verdict: Verdict;
  corrected_issue_type?: IssueType;
  corrected_root_cause?: RootCause;
  hint?: string;
  notes?: string;
}

interface InvestigationEventBase {
  id: number;
  investigation_id: string;
  created_at: string;
}
export interface StepStartedEvent extends InvestigationEventBase {
  type: 'step_started';
  payload: { node: string; step?: number };
}
export interface ClassifiedEvent extends InvestigationEventBase {
  type: 'classified';
  payload: {
    issue_type: IssueType;
    confidence: number;
    method: 'verified' | 'triaged';
    previous_category?: IssueType | null;
    reasoning?: string;
  };
}
export interface ToolCallEvent extends InvestigationEventBase {
  type: 'tool_call';
  payload: { call_id: string; tool: string; args: Record<string, unknown> };
}
export interface EvidenceEvent extends InvestigationEventBase {
  type: 'evidence';
  payload: { call_id: string; evidence: Evidence };
}
export interface TokenEvent extends InvestigationEventBase {
  type: 'token';
  payload: { text: string };
}
export interface DiagnosisEvent extends InvestigationEventBase {
  type: 'diagnosis';
  payload: { diagnosis: Diagnosis };
}
export interface EscalatedEvent extends InvestigationEventBase {
  type: 'escalated';
  payload: { escalation: Escalation };
}
export interface AwaitingReviewEvent extends InvestigationEventBase {
  type: 'awaiting_review';
  payload: { has_diagnosis?: boolean };
}
export interface ReviewRecordedEvent extends InvestigationEventBase {
  type: 'review_recorded';
  payload: { review: Review };
}
export interface InvestigationStatusChangedEvent extends InvestigationEventBase {
  type: 'status_changed';
  payload: { status: InvestigationStatus; error?: string | null };
}
export type InvestigationEvent =
  | StepStartedEvent
  | ClassifiedEvent
  | ToolCallEvent
  | EvidenceEvent
  | TokenEvent
  | DiagnosisEvent
  | EscalatedEvent
  | AwaitingReviewEvent
  | ReviewRecordedEvent
  | InvestigationStatusChangedEvent;

// --- simulation ------------------------------------------------------------------------
export interface SimulationRequest {
  type?: IssueType;
}

export interface SimulatedCustomer {
  simulation_id: string;
  conversation_id: string;
  customer_id: string;
  customer_name?: string;
}
