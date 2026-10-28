package com.claudepoc.banking.rules;

import com.claudepoc.banking.domain.Account;
import com.claudepoc.banking.domain.Enums.CancelledBy;
import com.claudepoc.banking.domain.Enums.CollectionFailureCode;
import com.claudepoc.banking.domain.Enums.MandateStatus;
import com.claudepoc.banking.domain.Mandate;
import java.math.BigDecimal;
import java.util.Optional;

/** Rules for paying a direct debit collection on its due date, checked in this order. */
public final class CollectionRules {

    private CollectionRules() {
    }

    public static Optional<CollectionFailureCode> check(Mandate mandate, Account account, BigDecimal amount) {
        if (mandate.getStatus() == MandateStatus.DORMANT) {
            // A dormant mandate has lapsed: the bank no longer holds a valid instruction.
            return Optional.of(CollectionFailureCode.NO_MANDATE);
        }
        if (mandate.getStatus() == MandateStatus.CANCELLED) {
            // The originator closed the customer's account on its side (and cancelled the mandate),
            // yet a collection was still due: that is a different case from the customer cancelling.
            return Optional.of(mandate.getCancelledBy() == CancelledBy.ORIGINATOR
                    ? CollectionFailureCode.ACCOUNT_CLOSED_BY_ORIGINATOR
                    : CollectionFailureCode.MANDATE_CANCELLED);
        }
        if (!account.isActive()) {
            // The contract has no separate code for a frozen payer account; any non-active account
            // cannot be debited, which Bacs reports as the payer's account being closed.
            return Optional.of(CollectionFailureCode.PAYER_ACCOUNT_CLOSED);
        }
        if (amount.compareTo(account.available()) > 0) {
            return Optional.of(CollectionFailureCode.INSUFFICIENT_FUNDS);
        }
        return Optional.empty();
    }
}
