package com.claudepoc.banking.simulation;

import com.claudepoc.banking.domain.Enums.Wire;

/** Shared enums from contracts/scenario.schema.json ($defs IssueType, RootCause, ExpectedOutcome). */
public final class SimulationEnums {

    private SimulationEnums() {
    }

    public enum IssueType implements Wire { CARD_DECLINE, MISSED_PAYMENT, FAILED_DIRECT_DEBIT, OTHER }

    public enum RootCause implements Wire {
        CARD_FROZEN_BY_CUSTOMER,
        INSUFFICIENT_FUNDS,
        CARD_EXPIRED,
        CONTACTLESS_LIMIT_EXCEEDED,
        MERCHANT_CATEGORY_BLOCKED,
        FRAUD_RULE_TRIGGERED,
        DAILY_PAYMENT_LIMIT_EXCEEDED,
        PAYEE_ACCOUNT_CLOSED,
        MANDATE_CANCELLED_BY_CUSTOMER,
        ACCOUNT_CLOSED_BY_ORIGINATOR,
        ONLINE_PAYMENTS_DISABLED,
        CARD_LOST_OR_STOLEN,
        ACCOUNT_NOT_ACTIVE,
        INVALID_PAYEE_DETAILS,
        NO_MANDATE,
        PAYER_ACCOUNT_CLOSED,
        UNKNOWN
    }

    public enum ExpectedOutcome implements Wire { SELF_SERVE, NEEDS_HUMAN }
}
