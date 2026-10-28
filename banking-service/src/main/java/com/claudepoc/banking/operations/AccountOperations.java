package com.claudepoc.banking.operations;

import com.claudepoc.banking.domain.Account;
import com.claudepoc.banking.domain.Customer;
import com.claudepoc.banking.domain.DomainException;
import com.claudepoc.banking.domain.Enums.AccountStatus;
import com.claudepoc.banking.domain.Enums.CustomerStatus;
import com.claudepoc.banking.domain.Enums.PaymentFailureReason;
import com.claudepoc.banking.domain.Enums.TransactionType;
import com.claudepoc.banking.domain.Money;
import com.claudepoc.banking.domain.NotFoundException;
import com.claudepoc.banking.domain.Repositories.AccountRepository;
import com.claudepoc.banking.domain.Repositories.CustomerRepository;
import com.claudepoc.banking.domain.Repositories.TransactionRepository;
import com.claudepoc.banking.domain.Transaction;
import com.claudepoc.banking.rules.PaymentRules;
import java.math.BigDecimal;
import java.time.Instant;
import java.time.LocalDate;
import java.time.ZoneOffset;
import java.util.EnumSet;
import java.util.Optional;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.ThreadLocalRandom;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

/** Customers, accounts and the ledger: every balance change goes through {@link #book}. */
@Service
@Transactional
public class AccountOperations {

    public static final String BANK_SORT_CODE = "20-45-77";
    public static final BigDecimal DEFAULT_DAILY_PAYMENT_LIMIT = new BigDecimal("10000.00");
    /** Outgoing payments that count towards the daily payment limit. */
    private static final Set<TransactionType> LIMITED_TYPES =
            EnumSet.of(TransactionType.SCHEDULED_PAYMENT, TransactionType.TRANSFER_OUT);

    private final CustomerRepository customers;
    private final AccountRepository accounts;
    private final TransactionRepository transactions;

    public AccountOperations(CustomerRepository customers, AccountRepository accounts,
                             TransactionRepository transactions) {
        this.customers = customers;
        this.accounts = accounts;
        this.transactions = transactions;
    }

    public Customer onboardCustomer(String name, String email, boolean vulnerable, Instant at) {
        return onboardCustomer(UUID.randomUUID(), name, email, vulnerable, at);
    }

    public Customer onboardCustomer(UUID id, String name, String email, boolean vulnerable, Instant at) {
        return customers.save(new Customer(id, name, email, vulnerable, at));
    }

    public void changeCustomerStatus(UUID customerId, CustomerStatus status) {
        customer(customerId).setStatus(status);
    }

    public Account openAccount(UUID customerId, String name, BigDecimal overdraftLimit, Instant at) {
        Customer customer = customer(customerId);
        if (customer.getStatus() == CustomerStatus.CLOSED) {
            throw new DomainException("Customer " + customerId + " is closed");
        }
        String number;
        do {
            number = String.format("%08d", ThreadLocalRandom.current().nextInt(10_000_000, 100_000_000));
        } while (accounts.existsBySortCodeAndAccountNumber(BANK_SORT_CODE, number));
        return accounts.save(new Account(UUID.randomUUID(), customerId, name, BANK_SORT_CODE, number, "GBP",
                overdraftLimit, DEFAULT_DAILY_PAYMENT_LIMIT, at));
    }

    public void setOverdraftLimit(UUID accountId, BigDecimal limit) {
        account(accountId).setOverdraftLimit(limit);
    }

    /** The customer changes the account's daily limit for outgoing payments in the app. */
    public void setDailyPaymentLimit(UUID accountId, BigDecimal limit) {
        if (limit.signum() <= 0) {
            throw new DomainException("Daily payment limit must be positive");
        }
        account(accountId).setDailyPaymentLimit(limit);
    }

    public void changeAccountStatus(UUID accountId, AccountStatus status) {
        account(accountId).setStatus(status);
    }

    /** Money arrives from outside the bank (salary, a transfer from someone else). */
    public Transaction receiveTransfer(UUID accountId, BigDecimal amount, String description, Instant at) {
        Account account = account(accountId);
        if (account.getStatus() == AccountStatus.CLOSED) {
            throw new DomainException("Account " + accountId + " is closed");
        }
        return book(account, amount.abs(), TransactionType.TRANSFER_IN, description, null, at);
    }

    /**
     * The customer sends money out (one-off transfer). It follows the payment rules; a rejected
     * transfer is not booked and the reason is returned.
     */
    public Optional<PaymentFailureReason> sendTransfer(UUID accountId, BigDecimal amount, String description, Instant at) {
        Account account = account(accountId);
        Optional<PaymentFailureReason> failure =
                PaymentRules.check(account, null, Money.normalise(amount), paidOutOn(accountId, day(at), at));
        if (failure.isEmpty()) {
            book(account, amount.abs().negate(), TransactionType.TRANSFER_OUT, description, null, at);
        }
        return failure;
    }

    /** Books a transaction and moves the balance. Negative amounts are money out. */
    public Transaction book(Account account, BigDecimal signedAmount, TransactionType type, String description,
                            UUID referenceId, Instant at) {
        account.applyAmount(signedAmount);
        return transactions.save(new Transaction(UUID.randomUUID(), account.getId(), signedAmount,
                account.getCurrency(), type, description, referenceId, at));
    }

    /** Outgoing payments and transfers already booked on that day before {@code before}, as a positive sum. */
    public BigDecimal paidOutOn(UUID accountId, LocalDate day, Instant before) {
        Instant start = day.atStartOfDay(ZoneOffset.UTC).toInstant();
        Instant end = before.isBefore(start.plusSeconds(86_400)) ? before : start.plusSeconds(86_400);
        return transactions.findByAccountIdAndBookedAtBetween(accountId, start, end).stream()
                .filter(t -> LIMITED_TYPES.contains(t.getType()) && t.getAmount().signum() < 0)
                .map(t -> t.getAmount().negate())
                .reduce(Money.ZERO, BigDecimal::add);
    }

    public Customer customer(UUID customerId) {
        return customers.findById(customerId)
                .orElseThrow(() -> new NotFoundException("Customer " + customerId + " not found"));
    }

    public Account account(UUID accountId) {
        return accounts.findById(accountId)
                .orElseThrow(() -> new NotFoundException("Account " + accountId + " not found"));
    }

    static LocalDate day(Instant at) {
        return LocalDate.ofInstant(at, ZoneOffset.UTC);
    }
}
