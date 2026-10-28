package com.claudepoc.banking.domain;

/**
 * Domain enums. Constant names are stored in the database; {@link Wire#wire()} gives the
 * contract value (lower case for statuses, the code itself for reason codes).
 */
public final class Enums {

    private Enums() {
    }

    /** Lower-case contract value of a status enum. */
    public interface Wire {
        default String wire() {
            return ((Enum<?>) this).name().toLowerCase(java.util.Locale.ROOT);
        }
    }

    /** A reason code: the contract value is the constant name. */
    public interface Code extends Wire {
        @Override
        default String wire() {
            return ((Enum<?>) this).name();
        }
    }

    public enum CustomerStatus implements Wire { ACTIVE, RESTRICTED, CLOSED }

    public enum AccountStatus implements Wire { ACTIVE, FROZEN, CLOSED }

    public enum CardBrand implements Wire { VISA, MASTERCARD }

    public enum CardStatus implements Wire { ACTIVE, FROZEN, BLOCKED, EXPIRED }

    public enum CardStatusReason implements Code { FROZEN_BY_CUSTOMER, FRAUD_SUSPECTED, LOST_OR_STOLEN, EXPIRED }

    public enum Channel implements Wire { CHIP_AND_PIN, CONTACTLESS, ONLINE }

    public enum AuthorisationResult implements Wire { APPROVED, DECLINED }

    public enum DeclineReason implements Code {
        CARD_FROZEN,
        CARD_BLOCKED,
        CARD_EXPIRED,
        INSUFFICIENT_FUNDS,
        CONTACTLESS_LIMIT_EXCEEDED,
        MERCHANT_CATEGORY_BLOCKED,
        ONLINE_PAYMENTS_DISABLED,
        FRAUD_RULE_TRIGGERED,
        ACCOUNT_NOT_ACTIVE
    }

    public enum FraudFlagStatus implements Wire { OPEN, CLEARED, CONFIRMED_FRAUD }

    public enum FraudAction implements Wire { CARD_BLOCKED, REVIEW_ONLY }

    public enum TransactionType implements Wire {
        CARD_PAYMENT, SCHEDULED_PAYMENT, DIRECT_DEBIT, TRANSFER_IN, TRANSFER_OUT, FEE, REFUND
    }

    public enum PayeeAccountStatus implements Wire { OPEN, CLOSED, UNKNOWN }

    public enum Frequency implements Wire { ONCE, WEEKLY, MONTHLY }

    public enum ScheduledPaymentStatus implements Wire { ACTIVE, PAUSED, CANCELLED, COMPLETED }

    public enum PaymentAttemptResult implements Wire { SUCCEEDED, FAILED, SKIPPED }

    public enum PaymentFailureReason implements Code {
        INSUFFICIENT_FUNDS,
        DAILY_PAYMENT_LIMIT_EXCEEDED,
        PAYEE_ACCOUNT_CLOSED,
        INVALID_PAYEE_DETAILS,
        ACCOUNT_NOT_ACTIVE
    }

    public enum MandateStatus implements Wire { ACTIVE, CANCELLED, DORMANT }

    public enum CancelledBy implements Wire { CUSTOMER, ORIGINATOR, BANK }

    public enum CollectionResult implements Wire { PENDING, COLLECTED, FAILED }

    public enum CollectionFailureCode implements Code {
        MANDATE_CANCELLED,
        NO_MANDATE,
        INSUFFICIENT_FUNDS,
        ACCOUNT_CLOSED_BY_ORIGINATOR,
        PAYER_ACCOUNT_CLOSED
    }

    /** Parses a contract value (case-insensitive) into the enum, or null when it is not one. */
    public static <E extends Enum<E> & Wire> E fromWire(Class<E> type, String value) {
        if (value == null) {
            return null;
        }
        for (E e : type.getEnumConstants()) {
            if (e.wire().equalsIgnoreCase(value.trim())) {
                return e;
            }
        }
        return null;
    }
}
