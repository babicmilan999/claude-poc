package com.claudepoc.banking.situations;

import com.claudepoc.banking.domain.Account;
import com.claudepoc.banking.domain.Card;
import com.claudepoc.banking.domain.CardAuthorisation;
import com.claudepoc.banking.domain.Customer;
import com.claudepoc.banking.domain.DirectDebitCollection;
import com.claudepoc.banking.domain.Enums.CardBrand;
import com.claudepoc.banking.domain.Enums.CardStatus;
import com.claudepoc.banking.domain.Enums.Channel;
import com.claudepoc.banking.domain.Enums.CollectionFailureCode;
import com.claudepoc.banking.domain.Enums.CustomerStatus;
import com.claudepoc.banking.domain.Enums.DeclineReason;
import com.claudepoc.banking.domain.Enums.PayeeAccountStatus;
import com.claudepoc.banking.domain.Enums.PaymentAttemptResult;
import com.claudepoc.banking.domain.Enums.PaymentFailureReason;
import com.claudepoc.banking.domain.Enums.Frequency;
import com.claudepoc.banking.domain.Mandate;
import com.claudepoc.banking.domain.Money;
import com.claudepoc.banking.domain.NotFoundException;
import com.claudepoc.banking.domain.Payee;
import com.claudepoc.banking.domain.PaymentAttempt;
import com.claudepoc.banking.domain.Repositories.AccountRepository;
import com.claudepoc.banking.domain.Repositories.CardRepository;
import com.claudepoc.banking.domain.ScheduledPayment;
import com.claudepoc.banking.operations.AccountOperations;
import com.claudepoc.banking.operations.AuthorisationOperations;
import com.claudepoc.banking.operations.AuthorisationOperations.AuthorisationRequest;
import com.claudepoc.banking.operations.CardOperations;
import com.claudepoc.banking.operations.DirectDebitOperations;
import com.claudepoc.banking.operations.PaymentOperations;
import com.claudepoc.banking.rules.FraudRules;
import java.math.BigDecimal;
import java.time.Instant;
import java.time.LocalDate;
import java.time.temporal.TemporalAdjusters;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.ThreadLocalRandom;
import org.springframework.stereotype.Component;

/**
 * Builds customer situations using only the domain operations: onboarding, card payments that
 * go through authorisation, standing orders that run, direct debits that are processed. Each
 * situation then checks that the rules produced the intended reason code and fails otherwise.
 *
 * <p>Used by the seed data and by the simulation module. Situations know nothing about expected
 * root causes or outcomes; that answer key lives only in the simulation module.
 */
@Component
public class Situations {

    public record Subject(Customer customer, Account account) {
    }

    private static final List<String> FIRST_NAMES = List.of("Sam", "Priya", "Tom", "Aisha", "Liam", "Zoe", "Omar",
            "Freya", "Noah", "Maya", "Ethan", "Ruby", "Leo", "Isla", "Jacob", "Amira");
    private static final List<String> LAST_NAMES = List.of("Taylor", "Patel", "Walker", "Khan", "Wright", "Hughes",
            "Ali", "Green", "Clarke", "Shah", "Turner", "Price", "Bennett", "Murphy", "Lewis", "Okafor");

    private final AccountOperations accounts;
    private final CardOperations cards;
    private final AuthorisationOperations authorisations;
    private final PaymentOperations payments;
    private final DirectDebitOperations directDebits;
    private final AccountRepository accountRepository;
    private final CardRepository cardRepository;

    public Situations(AccountOperations accounts, CardOperations cards, AuthorisationOperations authorisations,
                      PaymentOperations payments, DirectDebitOperations directDebits,
                      AccountRepository accountRepository, CardRepository cardRepository) {
        this.accounts = accounts;
        this.cards = cards;
        this.authorisations = authorisations;
        this.payments = payments;
        this.directDebits = directDebits;
        this.accountRepository = accountRepository;
        this.cardRepository = cardRepository;
    }

    // ---------------------------------------------------------------- subjects

    /** A fresh test customer with a random name. */
    public Subject newTestCustomer(Timeline t) {
        String first = pick(FIRST_NAMES);
        String last = pick(LAST_NAMES);
        String email = (first + "." + last + "." + ThreadLocalRandom.current().nextInt(1000, 10000) + "@example.com")
                .toLowerCase(java.util.Locale.ROOT);
        return newCustomer(UUID.randomUUID(), first + " " + last, email, false, t);
    }

    /**
     * A customer with a current account, a card and about four months of ordinary history:
     * an opening deposit, monthly salary and everyday card payments (all authorised by the rules).
     */
    public Subject newCustomer(UUID id, String name, String email, boolean vulnerable, Timeline t) {
        Customer customer = accounts.onboardCustomer(id, name, email, vulnerable, t.daysAgo(120, 9));
        Account account = accounts.openAccount(customer.getId(), "Current account", Money.ZERO, t.daysAgo(120, 9));
        accounts.receiveTransfer(account.getId(), new BigDecimal("500.00"), "Opening deposit", t.daysAgo(120, 10));
        LocalDate expiry = t.today().plusYears(3).with(TemporalAdjusters.lastDayOfMonth());
        Card card = cards.issueCard(account.getId(), pick(List.of(CardBrand.VISA, CardBrand.MASTERCARD)), expiry,
                t.daysAgo(118, 9));
        for (int monthsAgo = 3; monthsAgo >= 1; monthsAgo--) {
            accounts.receiveTransfer(account.getId(), new BigDecimal("2150.00"), "Salary ACME LTD",
                    t.daysAgo(30 * monthsAgo, 8));
        }
        Subject subject = new Subject(customer, account);
        spend(card, 27, "Pret A Manger", "5814", Channel.CONTACTLESS, "6.45", t);
        spend(card, 21, "Tesco Express", "5411", Channel.CONTACTLESS, "18.20", t);
        spend(card, 16, "TfL Travel", "4111", Channel.CONTACTLESS, "7.80", t);
        spend(card, 12, "Amazon UK", "5999", Channel.ONLINE, "34.99", t);
        spend(card, 8, "Sainsbury's", "5411", Channel.CHIP_AND_PIN, "54.10", t);
        spend(card, 4, "Costa Coffee", "5814", Channel.CONTACTLESS, "4.20", t);
        return subject;
    }

    /** An existing customer and their first active account (a new one is opened if they have none). */
    public Subject existingCustomer(UUID customerId, Timeline t) {
        Customer customer;
        try {
            customer = accounts.customer(customerId);
        } catch (NotFoundException e) {
            throw new SituationException("Unknown customer_id " + customerId);
        }
        if (customer.getStatus() != CustomerStatus.ACTIVE) {
            throw new SituationException("Customer " + customerId + " is " + customer.getStatus().wire());
        }
        Account account = accountRepository.findByCustomerIdOrderByOpenedAtAsc(customerId).stream()
                .filter(Account::isActive)
                .findFirst()
                .orElseGet(() -> {
                    Account opened = accounts.openAccount(customerId, "Current account", Money.ZERO, t.daysAgo(30, 9));
                    accounts.receiveTransfer(opened.getId(), new BigDecimal("1500.00"), "Transfer from savings",
                            t.daysAgo(30, 10));
                    return opened;
                });
        return new Subject(customer, account);
    }

    /** The account's active, unexpired card, or a newly issued one. */
    public Card activeCard(Subject s, Timeline t) {
        return cardRepository.findByAccountIdInOrderByIssuedAtDesc(List.of(s.account().getId())).stream()
                .filter(c -> c.getStatus() == CardStatus.ACTIVE && !c.isPastExpiry(t.today()))
                .findFirst()
                .orElseGet(() -> cards.issueCard(s.account().getId(), CardBrand.VISA,
                        t.today().plusYears(3).with(TemporalAdjusters.lastDayOfMonth()), t.daysAgo(10, 9)));
    }

    // ---------------------------------------------------------------- card declines

    /** The customer froze the card in the app, then tried to pay with it. */
    public SituationResult cardFrozenByCustomer(Subject s, SituationParams p, Timeline t) {
        Card card = activeCard(s, t);
        String merchant = p.string("merchant_name", "Tesco Express");
        String mcc = p.mcc("merchant_mcc", "5411");
        BigDecimal amount = p.money("amount", new BigDecimal("23.45"));
        int attempts = p.integer("declined_attempts", 2, 1, 5);
        cards.freezeByCustomer(card.getId());
        for (int i = 0; i < attempts; i++) {
            expectDecline(authorise(card, merchant, mcc, Channel.CHIP_AND_PIN, amount,
                    t.earlierToday(0.5 + 0.4 * i / attempts)), DeclineReason.CARD_FROZEN);
        }
        return card(s, card, facts("merchant_name", merchant, "amount", Money.format(amount),
                "declined_attempts", String.valueOf(attempts), "card_last4", card.getLast4()));
    }

    /** A card payment larger than the balance plus overdraft. */
    public SituationResult cardInsufficientFunds(Subject s, SituationParams p, Timeline t) {
        Card card = activeCard(s, t);
        String merchant = p.string("merchant_name", "Currys");
        String mcc = p.mcc("merchant_mcc", "5732");
        Account account = accounts.account(s.account().getId());
        BigDecimal amount = p.money("amount", account.available().add(randomAmount(20, 80)));
        if (amount.compareTo(account.available()) <= 0) {
            throw new SituationException("params.amount must be more than the available balance "
                    + Money.format(account.available()));
        }
        int attempts = p.integer("declined_attempts", 1, 1, 3);
        for (int i = 0; i < attempts; i++) {
            expectDecline(authorise(card, merchant, mcc, Channel.CHIP_AND_PIN, amount,
                    t.earlierToday(0.6 + 0.3 * i / attempts)), DeclineReason.INSUFFICIENT_FUNDS);
        }
        return card(s, card, facts("merchant_name", merchant, "amount", Money.format(amount),
                "declined_attempts", String.valueOf(attempts), "card_last4", card.getLast4()));
    }

    /**
     * An old card that expired at the end of last month (a replacement was issued and sits
     * unused), still being used after the nightly expiry run.
     */
    public SituationResult cardExpired(Subject s, SituationParams p, Timeline t) {
        String merchant = p.string("merchant_name", "Sainsbury's");
        String mcc = p.mcc("merchant_mcc", "5411");
        BigDecimal amount = p.money("amount", new BigDecimal("12.80"));
        LocalDate expiry = t.today().withDayOfMonth(1).minusDays(1);
        Card old = cards.issueCard(s.account().getId(), CardBrand.VISA, expiry,
                t.on(expiry.minusYears(3).withDayOfMonth(1), 9));
        expectApproved(authorise(old, "Sainsbury's", "5411", Channel.CONTACTLESS, new BigDecimal("9.60"),
                t.on(expiry.minusDays(3), 12)));
        cards.expireCards(t.today());
        expectDecline(authorise(old, merchant, mcc, Channel.CONTACTLESS, amount, t.earlierToday(0.7)),
                DeclineReason.CARD_EXPIRED);
        return card(s, old, facts("merchant_name", merchant, "amount", Money.format(amount),
                "card_last4", old.getLast4(), "expiry", expiry.toString()));
    }

    /** The customer lowered the contactless limit in the app, then tapped for more than it. */
    public SituationResult contactlessLimitExceeded(Subject s, SituationParams p, Timeline t) {
        Card card = activeCard(s, t);
        BigDecimal limit = p.money("contactless_limit", new BigDecimal("50.00"));
        String merchant = p.string("merchant_name", "The Kings Arms");
        String mcc = p.mcc("merchant_mcc", "5813");
        BigDecimal amount = p.money("amount", limit.add(new BigDecimal("18.40")));
        if (amount.compareTo(limit) <= 0) {
            throw new SituationException("params.amount must be more than the contactless limit " + Money.format(limit));
        }
        cards.setContactlessLimit(card.getId(), limit);
        expectDecline(authorise(card, merchant, mcc, Channel.CONTACTLESS, amount, t.earlierToday(0.8)),
                DeclineReason.CONTACTLESS_LIMIT_EXCEEDED);
        return card(s, card, facts("merchant_name", merchant, "amount", Money.format(amount),
                "card_last4", card.getLast4()));
    }

    /** The customer blocked a merchant category (gambling by default), then paid such a merchant online. */
    public SituationResult merchantCategoryBlocked(Subject s, SituationParams p, Timeline t) {
        Card card = activeCard(s, t);
        String mcc = p.mcc("merchant_mcc", "7995");
        String merchant = p.string("merchant_name", "LuckyStar Bets");
        BigDecimal amount = p.money("amount", new BigDecimal("20.00"));
        cards.setOnlinePayments(card.getId(), true);
        cards.blockMerchantCategory(card.getId(), mcc);
        expectDecline(authorise(card, merchant, mcc, Channel.ONLINE, amount, t.earlierToday(0.75)),
                DeclineReason.MERCHANT_CATEGORY_BLOCKED);
        return card(s, card, facts("merchant_name", merchant, "amount", Money.format(amount),
                "merchant_mcc", mcc, "card_last4", card.getLast4()));
    }

    /**
     * A high-value online payment to a crypto exchange trips fraud rule FR-12: declined, fraud
     * flag raised, card blocked. A later shop payment is then declined as CARD_BLOCKED.
     */
    public SituationResult fraudRuleTriggered(Subject s, SituationParams p, Timeline t) {
        Card card = activeCard(s, t);
        String merchant = p.string("merchant_name", "CoinVault Exchange");
        String mcc = p.mcc("merchant_mcc", "6051");
        BigDecimal amount = p.money("amount", new BigDecimal("1250.00"));
        if (!FraudRules.HIGH_RISK_MCCS.contains(mcc) || amount.compareTo(FraudRules.HIGH_RISK_ONLINE_THRESHOLD) < 0) {
            throw new SituationException("The fraud scenario needs a high-risk merchant_mcc " + FraudRules.HIGH_RISK_MCCS
                    + " and an amount of at least " + FraudRules.HIGH_RISK_ONLINE_THRESHOLD);
        }
        cards.setOnlinePayments(card.getId(), true);
        CardAuthorisation hit = authorise(card, merchant, mcc, Channel.ONLINE, amount, t.earlierToday(0.4));
        expectDecline(hit, DeclineReason.FRAUD_RULE_TRIGGERED);
        expectDecline(authorise(card, "Tesco Express", "5411", Channel.CHIP_AND_PIN, new BigDecimal("18.20"),
                t.earlierToday(0.8)), DeclineReason.CARD_BLOCKED);
        return card(s, card, facts("merchant_name", merchant, "amount", Money.format(amount),
                "fraud_rule_id", hit.getFraudRuleId(), "card_last4", card.getLast4()));
    }

    // ---------------------------------------------------------------- missed payments

    /** Monthly rent standing order that ran twice; due today; the customer's spending left too little. */
    public SituationResult missedPaymentInsufficientFunds(Subject s, SituationParams p, Timeline t) {
        BigDecimal amount = p.money("amount", new BigDecimal("850.00"));
        ScheduledPayment sp = standingOrderWithHistory(s, p.string("payee_name", "Oakwood Lettings"), "RENT", amount, t);
        drainBelow(s, amount, t);
        expectFailure(payments.runDue(sp.getId(), t.earlierToday(0.25)), PaymentFailureReason.INSUFFICIENT_FUNDS);
        expectFailure(payments.retryFailedRun(sp.getId(), t.earlierToday(0.6)), PaymentFailureReason.INSUFFICIENT_FUNDS);
        return payment(s, sp, p, amount);
    }

    /** The customer lowered the daily payment limit and moved money to savings first thing today. */
    public SituationResult missedPaymentDailyLimit(Subject s, SituationParams p, Timeline t) {
        BigDecimal amount = p.money("amount", new BigDecimal("850.00"));
        BigDecimal limit = p.money("daily_limit", new BigDecimal("1000.00"));
        ScheduledPayment sp = standingOrderWithHistory(s, p.string("payee_name", "Oakwood Lettings"), "RENT", amount, t);
        accounts.setDailyPaymentLimit(s.account().getId(), limit);
        BigDecimal transfer = Money.ZERO;
        if (amount.compareTo(limit) <= 0) {
            transfer = limit.subtract(amount).add(new BigDecimal("150.00")).min(limit);
            topUp(s, transfer.add(amount), t);
            var rejected = accounts.sendTransfer(s.account().getId(), transfer, "Transfer to savings", t.earlierToday(0.1));
            if (rejected.isPresent()) {
                throw new SituationException("The transfer to savings was rejected: " + rejected.get());
            }
        }
        expectFailure(payments.runDue(sp.getId(), t.earlierToday(0.3)), PaymentFailureReason.DAILY_PAYMENT_LIMIT_EXCEEDED);
        SituationResult result = payment(s, sp, p, amount);
        result.facts().put("transfer_amount", Money.format(transfer));
        result.facts().put("daily_limit", Money.format(limit));
        return result;
    }

    /** The landlord's bank reported the payee account closed; the standing order then fails. */
    public SituationResult missedPaymentPayeeClosed(Subject s, SituationParams p, Timeline t) {
        BigDecimal amount = p.money("amount", new BigDecimal("850.00"));
        ScheduledPayment sp = standingOrderWithHistory(s, p.string("payee_name", "Oakwood Lettings"), "RENT", amount, t);
        topUp(s, amount, t);
        payments.recordPayeeAccountStatus(sp.getPayeeId(), PayeeAccountStatus.CLOSED);
        expectFailure(payments.runDue(sp.getId(), t.earlierToday(0.3)), PaymentFailureReason.PAYEE_ACCOUNT_CLOSED);
        return payment(s, sp, p, amount);
    }

    // ---------------------------------------------------------------- direct debits

    /** The customer cancelled the mandate while tidying up direct debits; the originator collected anyway. */
    public SituationResult directDebitMandateCancelled(Subject s, SituationParams p, Timeline t) {
        String originator = p.string("originator", "Acme Lettings");
        BigDecimal amount = p.money("amount", new BigDecimal("950.00"));
        Mandate mandate = mandateWithHistory(s, originator, amount, t);
        directDebits.cancelByCustomer(mandate.getId(), t.daysAgo(20, 19));
        DirectDebitCollection due = directDebits.present(mandate.getId(), amount, t.today(), t.daysAgo(3, 9));
        expectFailure(directDebits.process(due.getId(), t.earlierToday(0.3)), CollectionFailureCode.MANDATE_CANCELLED);
        return mandate(s, mandate, originator, amount);
    }

    /** A direct debit collected on a day the account did not have enough money. */
    public SituationResult directDebitInsufficientFunds(Subject s, SituationParams p, Timeline t) {
        String originator = p.string("originator", "Northern Power");
        BigDecimal amount = p.money("amount", new BigDecimal("120.00"));
        Mandate mandate = mandateWithHistory(s, originator, amount, t);
        DirectDebitCollection due = directDebits.present(mandate.getId(), amount, t.today(), t.daysAgo(3, 9));
        drainBelow(s, amount, t);
        expectFailure(directDebits.process(due.getId(), t.earlierToday(0.3)), CollectionFailureCode.INSUFFICIENT_FUNDS);
        return mandate(s, mandate, originator, amount);
    }

    /**
     * The originator closed the customer's account on its side and cancelled the mandate after
     * presenting this month's collection, which then fails with ACCOUNT_CLOSED_BY_ORIGINATOR.
     */
    public SituationResult directDebitAccountClosedByOriginator(Subject s, SituationParams p, Timeline t) {
        String originator = p.string("originator", "Brightline Broadband");
        BigDecimal amount = p.money("amount", new BigDecimal("38.00"));
        Mandate mandate = mandateWithHistory(s, originator, amount, t);
        DirectDebitCollection due = directDebits.present(mandate.getId(), amount, t.today(), t.daysAgo(3, 9));
        directDebits.originatorClosedAccount(mandate.getId(), t.daysAgo(1, 16));
        expectFailure(directDebits.process(due.getId(), t.earlierToday(0.3)), CollectionFailureCode.ACCOUNT_CLOSED_BY_ORIGINATOR);
        return mandate(s, mandate, originator, amount);
    }

    // ---------------------------------------------------------------- healthy building blocks

    /** A monthly standing order that has run twice and is due today. */
    public ScheduledPayment standingOrderWithHistory(Subject s, String payeeName, String reference, BigDecimal amount,
                                                     Timeline t) {
        Payee payee = payments.addPayee(s.customer().getId(), payeeName, randomSortCode(), randomAccountNumber());
        LocalDate first = t.today().minusMonths(2);
        ScheduledPayment sp = payments.schedule(s.account().getId(), payee.getId(), reference, amount, Frequency.MONTHLY,
                first, t.on(first.minusDays(10), 18));
        topUp(s, amount, t);
        payments.runDue(sp.getId(), t.on(first, 6));
        topUp(s, amount, t);
        payments.runDue(sp.getId(), t.on(payments.scheduledPayment(sp.getId()).getNextRun(), 6));
        return sp;
    }

    /** A mandate set up months ago with two collections already paid. */
    public Mandate mandateWithHistory(Subject s, String originator, BigDecimal amount, Timeline t) {
        String reference = originator.replaceAll("[^A-Za-z]", "").toUpperCase(java.util.Locale.ROOT)
                .substring(0, Math.min(6, originator.replaceAll("[^A-Za-z]", "").length()))
                + "-" + ThreadLocalRandom.current().nextInt(100000, 1000000);
        Mandate mandate = directDebits.setUpMandate(s.account().getId(), originator, reference, t.daysAgo(100, 11));
        for (int monthsAgo = 2; monthsAgo >= 1; monthsAgo--) {
            LocalDate due = t.today().minusMonths(monthsAgo);
            topUp(s, amount, t);
            DirectDebitCollection c = directDebits.present(mandate.getId(), amount, due, t.on(due.minusDays(3), 9));
            directDebits.process(c.getId(), t.on(due, 6));
        }
        return mandate;
    }

    /** An approved card payment (fails the situation if the rules decline it). */
    public CardAuthorisation spend(Card card, int daysAgo, String merchant, String mcc, Channel channel, String amount,
                                   Timeline t) {
        return expectApproved(authorise(card, merchant, mcc, channel, new BigDecimal(amount), t.daysAgo(daysAgo, 13)));
    }

    // ---------------------------------------------------------------- helpers

    private CardAuthorisation authorise(Card card, String merchant, String mcc, Channel channel, BigDecimal amount,
                                        Instant at) {
        return authorisations.authorise(new AuthorisationRequest(card.getId(), amount, merchant, mcc, channel, at));
    }

    /** Spends with the card over the last few days so that the available balance ends below {@code amount}. */
    private void drainBelow(Subject s, BigDecimal amount, Timeline t) {
        Account account = accounts.account(s.account().getId());
        BigDecimal toSpend = account.available().subtract(amount).add(randomAmount(25, 90));
        if (toSpend.signum() <= 0) {
            return;
        }
        Card card = activeCard(s, t);
        BigDecimal first = Money.normalise(toSpend.multiply(new BigDecimal("0.6")));
        BigDecimal second = toSpend.subtract(first);
        expectApproved(authorise(card, "British Airways", "4511", Channel.ONLINE, first, t.daysAgo(3, 20)));
        if (second.signum() > 0) {
            expectApproved(authorise(card, "Currys", "5732", Channel.CHIP_AND_PIN, second, t.daysAgo(2, 15)));
        }
    }

    /** Makes sure the account can pay {@code amount}, with a transfer in from the customer's other bank. */
    private void topUp(Subject s, BigDecimal amount, Timeline t) {
        Account account = accounts.account(s.account().getId());
        if (account.available().compareTo(amount) < 0) {
            accounts.receiveTransfer(account.getId(), amount.subtract(account.available()).add(new BigDecimal("200.00")),
                    "Transfer from savings", t.daysAgo(5, 10));
        }
    }

    private static CardAuthorisation expectDecline(CardAuthorisation a, DeclineReason expected) {
        if (a.getDeclineReason() != expected) {
            throw new SituationException("Expected the card payment to be declined with " + expected
                    + " but the rules gave " + (a.getDeclineReason() == null ? "approved" : a.getDeclineReason()));
        }
        return a;
    }

    private static CardAuthorisation expectApproved(CardAuthorisation a) {
        if (a.getDeclineReason() != null) {
            throw new SituationException("A setup card payment was declined with " + a.getDeclineReason());
        }
        return a;
    }

    private static void expectFailure(PaymentAttempt attempt, PaymentFailureReason expected) {
        if (attempt.getResult() != PaymentAttemptResult.FAILED || attempt.getFailureReason() != expected) {
            throw new SituationException("Expected the scheduled payment to fail with " + expected
                    + " but it was " + attempt.getResult().wire()
                    + (attempt.getFailureReason() == null ? "" : " (" + attempt.getFailureReason() + ")"));
        }
    }

    private static void expectFailure(DirectDebitCollection c, CollectionFailureCode expected) {
        if (c.getFailureCode() != expected) {
            throw new SituationException("Expected the collection to fail with " + expected + " but it was "
                    + c.getResult().wire() + (c.getFailureCode() == null ? "" : " (" + c.getFailureCode() + ")"));
        }
    }

    private static SituationResult card(Subject s, Card card, Map<String, String> facts) {
        return new SituationResult(s.account().getId(), card.getId(), null, null, facts);
    }

    private SituationResult payment(Subject s, ScheduledPayment sp, SituationParams p, BigDecimal amount) {
        Payee payee = payments.payee(sp.getPayeeId());
        return new SituationResult(s.account().getId(), null, sp.getId(), null,
                facts("payee_name", payee.getName(), "amount", Money.format(amount), "reference", sp.getReference()));
    }

    private static SituationResult mandate(Subject s, Mandate m, String originator, BigDecimal amount) {
        return new SituationResult(s.account().getId(), null, null, m.getId(),
                facts("originator", originator, "amount", Money.format(amount), "reference", m.getReference()));
    }

    private static Map<String, String> facts(String... pairs) {
        Map<String, String> map = new LinkedHashMap<>();
        for (int i = 0; i < pairs.length; i += 2) {
            map.put(pairs[i], pairs[i + 1]);
        }
        return map;
    }

    private static BigDecimal randomAmount(int min, int max) {
        return Money.normalise(BigDecimal.valueOf(ThreadLocalRandom.current().nextInt(min * 100, max * 100), 2));
    }

    private static String randomSortCode() {
        ThreadLocalRandom r = ThreadLocalRandom.current();
        return String.format("%02d-%02d-%02d", r.nextInt(10, 100), r.nextInt(0, 100), r.nextInt(0, 100));
    }

    private static String randomAccountNumber() {
        return String.format("%08d", ThreadLocalRandom.current().nextInt(10_000_000, 100_000_000));
    }

    private static <T> T pick(List<T> values) {
        return values.get(ThreadLocalRandom.current().nextInt(values.size()));
    }
}
