// Canned data for the mock agent service. This stands in for what the real service gets
// from the banking API and the LLM. Each script is the mock's own "world state" for one
// kind of incident; the mock investigation "finds" the root cause by reading it, the way
// the real copilot would by calling tools. None of it is ever returned as an answer key.

import { IssueType, OverviewCard, RootCause, Source } from '../models';

export interface EvidenceTemplate {
  tool: string;
  args: Record<string, unknown>;
  summary: string;
  data: unknown;
}

export interface IncidentScript {
  type: Exclude<IssueType, 'other'>;
  rootCause: RootCause;
  /** The assistant must hand off (a human action is needed). */
  needsHuman: boolean;
  cardStatus: OverviewCard['status'];
  balance: string;
  /** What the (simulated) customer says first, and an e-mail complaint. */
  opening: string;
  complaint: string;
  /** The assistant's self-serve answer, and the sources it cites. */
  answer: string;
  sources: Source[];
  handoffSummary: string;
  evidence: EvidenceTemplate[];
  explanation: string;
  recommendedAction: string;
  /** What a second pass finds when ops rejects with a hint. */
  alternative: RootCause;
}

const ARTICLE = (articleId: string, title: string): Source => ({
  kind: 'article',
  title,
  article_id: articleId,
  url: `/help/${articleId}`,
});
const ACCOUNT = (tool: string, title: string): Source => ({ kind: 'account_data', title, tool });

export const SCRIPTS: IncidentScript[] = [
  {
    type: 'card_decline',
    rootCause: 'card_frozen_by_customer',
    needsHuman: false,
    cardStatus: 'frozen',
    balance: '842.10',
    opening: 'My card keeps getting declined at the supermarket. Why?',
    complaint: 'My card was declined three times today at Tesco. I have money in the account. Please explain.',
    answer:
      'Your card ending 4821 is frozen, so payments are declined. You froze it in the app on Monday. ' +
      'To unfreeze it, open Cards, pick the card and switch off "Freeze card". Payments work again straight away.',
    sources: [ACCOUNT('get_my_cards', 'Your cards'), ARTICLE('why-cards-get-declined', 'Why cards get declined')],
    handoffSummary: 'Customer reports repeated card declines at a supermarket.',
    evidence: [
      {
        tool: 'get_card_authorisations',
        args: { card_id: 'card-****4821', days: 7 },
        summary: '3 declined authorisations at TESCO STORES, reason CARD_FROZEN',
        data: { declined: 3, reasons: ['CARD_FROZEN'] },
      },
      {
        tool: 'get_card',
        args: { card_id: 'card-****4821' },
        summary: 'Card ****4821 status frozen, frozen by the customer in the app',
        data: { status: 'frozen', frozen_by: 'customer' },
      },
    ],
    explanation:
      'The card is frozen. The customer froze it in the app, and every authorisation since was declined with CARD_FROZEN.',
    recommendedAction: 'Tell the customer how to unfreeze the card in the app.',
    alternative: 'card_lost_or_stolen',
  },
  {
    type: 'card_decline',
    rootCause: 'card_expired',
    needsHuman: false,
    cardStatus: 'expired',
    balance: '1210.55',
    opening: 'My card stopped working online yesterday.',
    complaint: 'My card has been refused by every online shop since yesterday.',
    answer:
      'Your card ending 4821 expired at the end of last month, so payments are declined. ' +
      'Your replacement card was sent on the 2nd; activate it in Cards once it arrives.',
    sources: [ACCOUNT('get_my_cards', 'Your cards'), ARTICLE('expired-cards-and-replacements', 'Expired cards and replacement cards')],
    handoffSummary: 'Customer reports online card payments failing.',
    evidence: [
      {
        tool: 'get_card_authorisations',
        args: { card_id: 'card-****4821', days: 7 },
        summary: '4 declined authorisations online, reason CARD_EXPIRED',
        data: { declined: 4, reasons: ['CARD_EXPIRED'] },
      },
      {
        tool: 'get_card',
        args: { card_id: 'card-****4821' },
        summary: 'Card ****4821 expired last month; replacement dispatched',
        data: { status: 'expired' },
      },
    ],
    explanation: 'The card has expired. All recent authorisations were declined with CARD_EXPIRED.',
    recommendedAction: 'Point the customer to the replacement card and how to activate it.',
    alternative: 'account_not_active',
  },
  {
    type: 'card_decline',
    rootCause: 'fraud_rule_triggered',
    needsHuman: true,
    cardStatus: 'blocked',
    balance: '2301.00',
    opening: 'My card was declined abroad and now nothing works. Can I talk to someone?',
    complaint: 'I am travelling and my card was blocked after one payment. I need it working today.',
    answer: '',
    sources: [],
    handoffSummary:
      'Card declined abroad, then blocked. Signals point to a fraud block that only ops can lift; handed off.',
    evidence: [
      {
        tool: 'get_card_authorisations',
        args: { card_id: 'card-****7710', days: 3 },
        summary: '1 declined authorisation in Lisbon, reason FRAUD_SUSPECTED; card blocked after',
        data: { declined: 1, reasons: ['FRAUD_SUSPECTED'] },
      },
      {
        tool: 'get_fraud_flags',
        args: { customer_id: 'cust-****' },
        summary: 'Active fraud flag: unusual location rule',
        data: { flags: [{ rule: 'unusual_location', active: true }] },
      },
    ],
    explanation:
      'A fraud rule (unusual location) declined a payment abroad and blocked the card. Only ops can lift the block after checking with the customer.',
    recommendedAction: 'Call the customer to confirm the payment, then lift the fraud block.',
    alternative: 'card_lost_or_stolen',
  },
  {
    type: 'missed_payment',
    rootCause: 'insufficient_funds',
    needsHuman: false,
    cardStatus: 'active',
    balance: '12.40',
    opening: 'My rent standing order did not go out this month.',
    complaint: 'My rent payment did not go out on the 1st and my landlord is chasing me.',
    answer:
      'Your standing order to "Landlord Ltd" for 950.00 tried to run on the 1st, but the balance was 12.40, ' +
      'so it was not paid. It will try again on the next run date, or you can send it now as a one-off payment.',
    sources: [
      ACCOUNT('get_my_scheduled_payments', 'Your scheduled payments'),
      ARTICLE('how-scheduled-payments-work', 'How standing orders and scheduled payments work'),
    ],
    handoffSummary: 'Customer reports a missed rent standing order.',
    evidence: [
      {
        tool: 'get_payment_attempts',
        args: { scheduled_payment_id: 'sp-****0193' },
        summary: 'Attempt on the 1st failed: INSUFFICIENT_FUNDS (950.00 due)',
        data: { result: 'failed', failure_reason: 'INSUFFICIENT_FUNDS' },
      },
      {
        tool: 'get_balance',
        args: { account_id: 'acc-****1234' },
        summary: 'Balance 12.40 at the run time, no overdraft',
        data: { balance: '12.40', overdraft_limit: '0.00' },
      },
    ],
    explanation: 'The standing order failed for insufficient funds: 950.00 was due and the balance was 12.40.',
    recommendedAction: 'Tell the customer the payment will retry, or how to pay it now.',
    alternative: 'daily_payment_limit_exceeded',
  },
  {
    type: 'missed_payment',
    rootCause: 'daily_payment_limit_exceeded',
    needsHuman: false,
    cardStatus: 'active',
    balance: '5400.00',
    opening: 'A scheduled payment to my builder failed but I have plenty of money.',
    complaint: 'My 6,000 payment to the builder did not go out although my balance was fine.',
    answer:
      'The payment to your builder was 6,000.00, above your daily payment limit of 5,000.00, so it was not sent. ' +
      'You can split it over two days or change your limit in Settings.',
    sources: [
      ACCOUNT('get_my_scheduled_payments', 'Your scheduled payments'),
      ARTICLE('daily-payment-limit', 'Daily payment limit'),
    ],
    handoffSummary: 'Customer reports a failed scheduled payment despite enough balance.',
    evidence: [
      {
        tool: 'get_payment_attempts',
        args: { scheduled_payment_id: 'sp-****0417' },
        summary: 'Attempt failed: DAILY_LIMIT_EXCEEDED (6000.00 > 5000.00)',
        data: { result: 'failed', failure_reason: 'DAILY_LIMIT_EXCEEDED' },
      },
      {
        tool: 'get_balance',
        args: { account_id: 'acc-****5521' },
        summary: 'Balance 5400.00, enough for the payment',
        data: { balance: '5400.00' },
      },
    ],
    explanation: 'The payment exceeded the daily payment limit; the balance was not the problem.',
    recommendedAction: 'Explain the limit and how to split the payment or raise the limit.',
    alternative: 'insufficient_funds',
  },
  {
    type: 'missed_payment',
    rootCause: 'payee_account_closed',
    needsHuman: true,
    cardStatus: 'active',
    balance: '3100.00',
    opening: 'My monthly payment to my mum bounced back. I want to speak to a person.',
    complaint: 'My standing order to my mother was returned. Nobody can tell me why.',
    answer: '',
    sources: [],
    handoffSummary: 'Standing order returned by the receiving bank; payee account appears closed. Handed off.',
    evidence: [
      {
        tool: 'get_payment_attempts',
        args: { scheduled_payment_id: 'sp-****2290' },
        summary: 'Attempt returned: PAYEE_ACCOUNT_CLOSED',
        data: { result: 'returned', failure_reason: 'PAYEE_ACCOUNT_CLOSED' },
      },
      {
        tool: 'get_payees',
        args: { account_id: 'acc-****8812' },
        summary: 'Payee "M. Haddad" sort code 20-**-**, last success 2 months ago',
        data: { payees: 1 },
      },
    ],
    explanation: "The receiving bank returned the payment because the payee's account is closed.",
    recommendedAction: 'Contact the customer to get new payee details and cancel the standing order.',
    alternative: 'invalid_payee_details',
  },
  {
    type: 'failed_direct_debit',
    rootCause: 'mandate_cancelled_by_customer',
    needsHuman: false,
    cardStatus: 'active',
    balance: '640.00',
    opening: 'My gym says my direct debit failed. What happened?',
    complaint: 'My gym membership direct debit failed and they charged me a fee.',
    answer:
      'The direct debit to "FitLife Gyms" failed because the mandate was cancelled in the app on the 3rd. ' +
      'To keep paying them, give the gym your details again so they can set up a new mandate.',
    sources: [
      ACCOUNT('get_my_direct_debits', 'Your direct debits'),
      ARTICLE('how-direct-debits-work', 'How Direct Debits and mandates work'),
    ],
    handoffSummary: 'Customer reports a failed gym direct debit.',
    evidence: [
      {
        tool: 'get_mandates',
        args: { account_id: 'acc-****3307' },
        summary: 'Mandate to FITLIFE GYMS cancelled by the customer on the 3rd',
        data: { status: 'cancelled', cancelled_by: 'customer' },
      },
      {
        tool: 'get_dd_collections',
        args: { mandate_id: 'ddm-****0042' },
        summary: 'Collection of 39.99 failed: NO_MANDATE',
        data: { result: 'failed', failure_code: 'NO_MANDATE' },
      },
    ],
    explanation: 'The customer cancelled the mandate, so the next collection failed with NO_MANDATE.',
    recommendedAction: 'Explain that the gym needs a new mandate.',
    alternative: 'insufficient_funds',
  },
  {
    type: 'failed_direct_debit',
    rootCause: 'account_closed_by_originator',
    needsHuman: true,
    cardStatus: 'active',
    balance: '980.00',
    opening: 'My energy direct debit failed and the supplier says it is our side. Please get someone to help.',
    complaint: 'My energy bill direct debit failed. The supplier blames the bank.',
    answer: '',
    sources: [],
    handoffSummary: 'Energy direct debit failed; the originator reports the account closed. Handed off.',
    evidence: [
      {
        tool: 'get_dd_collections',
        args: { mandate_id: 'ddm-****7781' },
        summary: 'Collection failed: ACCOUNT_CLOSED_BY_ORIGINATOR',
        data: { result: 'failed', failure_code: 'ACCOUNT_CLOSED_BY_ORIGINATOR' },
      },
      {
        tool: 'get_mandates',
        args: { account_id: 'acc-****6650' },
        summary: 'Mandate to BRIGHT ENERGY active',
        data: { status: 'active' },
      },
    ],
    explanation: "The originator closed the account the collection belongs to on their side; the mandate itself is active.",
    recommendedAction: 'Contact the supplier to reinstate the account, then tell the customer.',
    alternative: 'payer_account_closed',
  },
];

export function scriptFor(rootCause: RootCause): IncidentScript | undefined {
  return SCRIPTS.find((s) => s.rootCause === rootCause);
}

/** Rough keyword triage, standing in for the `triage` alias. */
export function classifyText(text: string): IssueType {
  const t = text.toLowerCase();
  if (/direct debit|mandate|\bdd\b/.test(t)) return 'failed_direct_debit';
  if (/standing order|scheduled|rent|bounced|payment (did not|didn't) go|missed|transfer/.test(t)) return 'missed_payment';
  if (/card|declin|refused|blocked/.test(t)) return 'card_decline';
  return 'other';
}

/** Rules that force a handoff, standing in for the assistant's handoff rules. */
export function asksForHuman(text: string): boolean {
  return /\b(human|person|someone|agent|complain|complaint|fraud)\b/i.test(text);
}

export const FIRST_NAMES = ['Grace', 'Hugo', 'Isla', 'Jonas', 'Keira', 'Liam', 'Maya', 'Noah', 'Olivia', 'Priya'];
export const LAST_NAMES = ['Patel', 'Reid', 'Silva', 'Turner', 'Ueda', 'Varga', 'Walsh', 'Young', 'Zhou', 'Adeyemi'];

export const GENERIC_ANSWER =
  'I can help with card payments, scheduled payments and direct debits. ' +
  'Could you tell me a bit more about the payment that went wrong?';
export const GENERIC_SOURCES: Source[] = [ARTICLE('contact-support', 'How to contact support')];
export const HANDOFF_NOTICE = 'An agent will look into this and get back to you.';
