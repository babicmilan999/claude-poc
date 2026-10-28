package com.claudepoc.banking.rules;

import com.claudepoc.banking.domain.Account;
import com.claudepoc.banking.domain.Enums.PayeeAccountStatus;
import com.claudepoc.banking.domain.Enums.PaymentFailureReason;
import com.claudepoc.banking.domain.Payee;
import java.math.BigDecimal;
import java.util.Optional;

/** Rules for outgoing payments (scheduled payments and transfers), checked in this order. */
public final class PaymentRules {

    private PaymentRules() {
    }

    /**
     * @param payee null for a transfer between the customer's own accounts
     * @param paidOutToday outgoing payments and transfers already made from the account that day (positive)
     */
    public static Optional<PaymentFailureReason> check(Account account, Payee payee, BigDecimal amount,
                                                       BigDecimal paidOutToday) {
        if (!account.isActive()) {
            return Optional.of(PaymentFailureReason.ACCOUNT_NOT_ACTIVE);
        }
        if (payee != null && payee.getAccountStatus() == PayeeAccountStatus.UNKNOWN) {
            // The receiving bank cannot find an account with these details.
            return Optional.of(PaymentFailureReason.INVALID_PAYEE_DETAILS);
        }
        if (payee != null && payee.getAccountStatus() == PayeeAccountStatus.CLOSED) {
            return Optional.of(PaymentFailureReason.PAYEE_ACCOUNT_CLOSED);
        }
        if (paidOutToday.add(amount).compareTo(account.getDailyPaymentLimit()) > 0) {
            return Optional.of(PaymentFailureReason.DAILY_PAYMENT_LIMIT_EXCEEDED);
        }
        if (amount.compareTo(account.available()) > 0) {
            return Optional.of(PaymentFailureReason.INSUFFICIENT_FUNDS);
        }
        return Optional.empty();
    }
}
