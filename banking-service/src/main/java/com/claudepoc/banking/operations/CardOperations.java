package com.claudepoc.banking.operations;

import com.claudepoc.banking.domain.Account;
import com.claudepoc.banking.domain.Card;
import com.claudepoc.banking.domain.DomainException;
import com.claudepoc.banking.domain.Enums.CardBrand;
import com.claudepoc.banking.domain.Enums.CardStatus;
import com.claudepoc.banking.domain.Enums.CardStatusReason;
import com.claudepoc.banking.domain.NotFoundException;
import com.claudepoc.banking.domain.Repositories.CardRepository;
import java.math.BigDecimal;
import java.time.Instant;
import java.time.LocalDate;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.ThreadLocalRandom;
import java.util.regex.Pattern;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

/** Card lifecycle and the controls the customer sets in the app. */
@Service
@Transactional
public class CardOperations {

    public static final BigDecimal DEFAULT_CONTACTLESS_LIMIT = new BigDecimal("100.00");
    private static final Pattern MCC = Pattern.compile("^\\d{4}$");

    private final CardRepository cards;
    private final AccountOperations accounts;

    public CardOperations(CardRepository cards, AccountOperations accounts) {
        this.cards = cards;
        this.accounts = accounts;
    }

    public Card issueCard(UUID accountId, CardBrand brand, LocalDate expiry, Instant at) {
        Account account = accounts.account(accountId);
        if (!account.isActive()) {
            throw new DomainException("Cannot issue a card on a non-active account");
        }
        String last4 = String.format("%04d", ThreadLocalRandom.current().nextInt(0, 10_000));
        return cards.save(new Card(UUID.randomUUID(), accountId, last4, brand, expiry, DEFAULT_CONTACTLESS_LIMIT, at));
    }

    /** The customer freezes the card in the app. */
    public void freezeByCustomer(UUID cardId) {
        Card card = card(cardId);
        requireStatus(card, CardStatus.ACTIVE);
        card.changeStatus(CardStatus.FROZEN, CardStatusReason.FROZEN_BY_CUSTOMER);
    }

    /** The customer unfreezes the card in the app. */
    public void unfreeze(UUID cardId) {
        Card card = card(cardId);
        requireStatus(card, CardStatus.FROZEN);
        card.changeStatus(CardStatus.ACTIVE, null);
    }

    public void reportLostOrStolen(UUID cardId) {
        Card card = card(cardId);
        if (card.getStatus() == CardStatus.EXPIRED) {
            throw new DomainException("Card " + cardId + " has expired");
        }
        card.changeStatus(CardStatus.BLOCKED, CardStatusReason.LOST_OR_STOLEN);
    }

    /** Fraud operations block a card after a fraud rule fires. Only ops can lift it. */
    public void blockForSuspectedFraud(UUID cardId) {
        Card card = card(cardId);
        if (card.getStatus() != CardStatus.EXPIRED) {
            card.changeStatus(CardStatus.BLOCKED, CardStatusReason.FRAUD_SUSPECTED);
        }
    }

    /** Nightly housekeeping: cards past their expiry date become expired. Returns how many changed. */
    public int expireCards(LocalDate today) {
        int changed = 0;
        for (Card card : cards.findAll()) {
            if (card.getStatus() != CardStatus.EXPIRED && card.isPastExpiry(today)) {
                card.changeStatus(CardStatus.EXPIRED, CardStatusReason.EXPIRED);
                changed++;
            }
        }
        return changed;
    }

    public void setContactlessLimit(UUID cardId, BigDecimal limit) {
        if (limit.signum() < 0) {
            throw new DomainException("Contactless limit cannot be negative");
        }
        card(cardId).setContactlessLimit(limit);
    }

    public void blockMerchantCategory(UUID cardId, String mcc) {
        requireMcc(mcc);
        Card card = card(cardId);
        List<String> blocked = new java.util.ArrayList<>(card.getBlockedMerchantCategories());
        if (!blocked.contains(mcc)) {
            blocked.add(mcc);
        }
        card.setBlockedMerchantCategories(blocked);
    }

    public void unblockMerchantCategory(UUID cardId, String mcc) {
        Card card = card(cardId);
        card.setBlockedMerchantCategories(card.getBlockedMerchantCategories().stream().filter(m -> !m.equals(mcc)).toList());
    }

    public void setOnlinePayments(UUID cardId, boolean enabled) {
        card(cardId).setOnlinePaymentsEnabled(enabled);
    }

    public Card card(UUID cardId) {
        return cards.findById(cardId).orElseThrow(() -> new NotFoundException("Card " + cardId + " not found"));
    }

    static void requireMcc(String mcc) {
        if (mcc == null || !MCC.matcher(mcc).matches()) {
            throw new DomainException("A merchant category code has four digits: " + mcc);
        }
    }

    private static void requireStatus(Card card, CardStatus expected) {
        if (card.getStatus() != expected) {
            throw new DomainException("Card " + card.getId() + " is " + card.getStatus().wire() + ", not " + expected.wire());
        }
    }
}
