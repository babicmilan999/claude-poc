package com.claudepoc.banking.rules;

import com.claudepoc.banking.domain.Account;
import com.claudepoc.banking.domain.Card;
import com.claudepoc.banking.domain.Enums.CardStatus;
import com.claudepoc.banking.domain.Enums.Channel;
import com.claudepoc.banking.domain.Enums.DeclineReason;
import java.math.BigDecimal;
import java.time.LocalDate;
import java.util.Optional;

/**
 * Card authorisation rules, checked in this order. The first rule that fails gives the decline
 * reason. Fraud rules ({@link FraudRules}) run after the card checks and before the funds check.
 */
public final class AuthorisationRules {

    private AuthorisationRules() {
    }

    /** Account, card status and card control checks. */
    public static Optional<DeclineReason> cardChecks(Account account, Card card, BigDecimal amount,
                                                     String mcc, Channel channel, LocalDate day) {
        if (!account.isActive()) {
            return Optional.of(DeclineReason.ACCOUNT_NOT_ACTIVE);
        }
        if (card.getStatus() == CardStatus.EXPIRED || card.isPastExpiry(day)) {
            return Optional.of(DeclineReason.CARD_EXPIRED);
        }
        if (card.getStatus() == CardStatus.BLOCKED) {
            return Optional.of(DeclineReason.CARD_BLOCKED);
        }
        if (card.getStatus() == CardStatus.FROZEN) {
            return Optional.of(DeclineReason.CARD_FROZEN);
        }
        if (channel == Channel.ONLINE && !card.isOnlinePaymentsEnabled()) {
            return Optional.of(DeclineReason.ONLINE_PAYMENTS_DISABLED);
        }
        if (card.getBlockedMerchantCategories().contains(mcc)) {
            return Optional.of(DeclineReason.MERCHANT_CATEGORY_BLOCKED);
        }
        if (channel == Channel.CONTACTLESS && amount.compareTo(card.getContactlessLimit()) > 0) {
            return Optional.of(DeclineReason.CONTACTLESS_LIMIT_EXCEEDED);
        }
        return Optional.empty();
    }

    /** Funds check: the amount must fit in the balance plus overdraft. */
    public static Optional<DeclineReason> fundsCheck(Account account, BigDecimal amount) {
        return amount.compareTo(account.available()) > 0
                ? Optional.of(DeclineReason.INSUFFICIENT_FUNDS)
                : Optional.empty();
    }
}
