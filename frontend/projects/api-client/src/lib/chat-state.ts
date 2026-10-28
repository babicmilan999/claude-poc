import { ChatEvent, ConversationDetail, ConversationOutcome, ConversationStatus, Message } from './models';

/** A message as shown in the chat; `streaming` while its tokens are still arriving. */
export interface ChatMessageView extends Message {
  streaming?: boolean;
}

export interface ChatState {
  messages: ChatMessageView[];
  status: ConversationStatus;
  outcome: ConversationOutcome | null;
  handoff: { case_id: string; notice: string } | null;
  error: string | null;
}

export const EMPTY_CHAT: ChatState = {
  messages: [],
  status: 'open',
  outcome: null,
  handoff: null,
  error: null,
};

export function chatStateFromDetail(detail: ConversationDetail): ChatState {
  return {
    ...EMPTY_CHAT,
    messages: [...detail.messages].sort((a, b) => a.id - b.id),
    status: detail.status,
    outcome: detail.outcome ?? null,
    handoff: detail.case_id && detail.outcome === 'handed_off' ? { case_id: detail.case_id, notice: '' } : null,
  };
}

function upsert(messages: ChatMessageView[], id: number, update: (m: ChatMessageView | undefined) => ChatMessageView) {
  const index = messages.findIndex((m) => m.id === id);
  const next = [...messages];
  if (index === -1) {
    next.push(update(undefined));
    next.sort((a, b) => a.id - b.id);
  } else {
    next[index] = update(next[index]);
  }
  return next;
}

function draft(conversationId: string, id: number, createdAt: string): ChatMessageView {
  return {
    id,
    conversation_id: conversationId,
    role: 'assistant',
    content: '',
    sources: [],
    rating: null,
    created_at: createdAt,
    streaming: true,
  };
}

/**
 * Applies one chat stream event. Used by the chat (POST stream) and by watch mode (the
 * conversation events stream, whose replay may repeat messages: they are matched by id).
 */
export function applyChatEvent(state: ChatState, event: ChatEvent): ChatState {
  switch (event.type) {
    case 'message_created':
      return {
        ...state,
        error: null,
        // A new customer message clears an earlier "resolved" (contract: rateMessage).
        outcome: event.message.role === 'customer' && state.outcome === 'resolved' ? null : state.outcome,
        messages: upsert(state.messages, event.message.id, () => ({ ...event.message })),
      };
    case 'token':
      return {
        ...state,
        messages: upsert(state.messages, event.message_id, (m) => {
          const base = m ?? draft(event.conversation_id, event.message_id, event.created_at);
          return { ...base, content: base.content + event.text, streaming: true };
        }),
      };
    case 'sources':
      return {
        ...state,
        messages: upsert(state.messages, event.message_id, (m) => ({
          ...(m ?? draft(event.conversation_id, event.message_id, event.created_at)),
          sources: event.sources,
        })),
      };
    case 'message_completed':
      return {
        ...state,
        messages: upsert(state.messages, event.message.id, () => ({ ...event.message, streaming: false })),
      };
    case 'handoff':
      return { ...state, handoff: { case_id: event.case_id, notice: event.notice } };
    case 'conversation_status':
      return { ...state, status: event.status, outcome: event.outcome ?? null };
    case 'error':
      return {
        ...state,
        error: event.message,
        messages: state.messages.map((m) => (m.streaming ? { ...m, streaming: false } : m)),
      };
    default:
      return state;
  }
}
