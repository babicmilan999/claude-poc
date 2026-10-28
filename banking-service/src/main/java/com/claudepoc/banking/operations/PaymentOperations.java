package com.claudepoc.banking.operations;

import com.claudepoc.banking.domain.Account;
import com.claudepoc.banking.domain.DomainException;
import com.claudepoc.banking.domain.Enums.Frequency;
import com.claudepoc.banking.domain.Enums.PayeeAccountStatus;
import com.claudepoc.banking.domain.Enums.PaymentAttemptResult;
import com.claudepoc.banking.domain.Enums.PaymentFailureReason;
import com.claudepoc.banking.domain.Enums.ScheduledPaymentStatus;
import com.claudepoc.banking.domain.Enums.TransactionType;
import com.claudepoc.banking.domain.Money;
import com.claudepoc.banking.domain.NotFoundException;
import com.claudepoc.banking.domain.Payee;
import com.claudepoc.banking.domain.PaymentAttempt;
import com.claudepoc.banking.domain.Repositories.PayeeRepository;
import com.claudepoc.banking.domain.Repositories.PaymentAttemptRepository;
import com.claudepoc.banking.domain.Repositories.ScheduledPaymentRepository;
import com.claudepoc.banking.domain.ScheduledPayment;
import com.claudepoc.banking.rules.PaymentRules;
import java.math.BigDecimal;
import java.time.Instant;
import java.time.LocalDate;
import java.util.List;
import java.util.Optional;
import java.util.UUID;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

/**
 * Payees, standing orders and scheduled payments. A run executes the payment for its next run
 * date through {@link PaymentRules}; a failed run can be retried the same day.
 */
@Service
@Transactional
public class PaymentOperations {

    /** Retries allowed for one run date after the first attempt. */
    public static final int MAX_RETRIES = 2;

    private final AccountOperations accounts;
    private final PayeeRepository payees;
    private final ScheduledPaymentRepository scheduledPayments;
    private final PaymentAttemptRepository attempts;

    public PaymentOperations(AccountOperations accounts, PayeeRepository payees,
                             ScheduledPaymentRepository scheduledPayments, PaymentAttemptRepository attempts) {
        this.accounts = accounts;
        this.payees = payees;
        this.scheduledPayments = scheduledPayments;
        this.attempts = attempts;
    }

    public Payee addPayee(UUID customerId, String name, String sortCode, String accountNumber) {
        accounts.customer(customerId);
        return payees.save(new Payee(UUID.randomUUID(), customerId, name, sortCode, accountNumber, PayeeAccountStatus.OPEN));
    }

    /** The receiving bank reports the status of the payee's account (for example that it was closed). */
    public void recordPayeeAccountStatus(UUID payeeId, PayeeAccountStatus status) {
        payee(payeeId).setAccountStatus(status);
    }

    public ScheduledPayment schedule(UUID accountId, UUID payeeId, String reference, BigDecimal amount,
                                     Frequency frequency, LocalDate firstRun, Instant at) {
        Account account = accounts.account(accountId);
        Payee payee = payee(payeeId);
        if (!payee.getCustomerId().equals(account.getCustomerId())) {
            throw new DomainException("Payee belongs to another customer");
        }
        if (amount.signum() <= 0) {
            throw new DomainException("Amount must be positive");
        }
        return scheduledPayments.save(new ScheduledPayment(UUID.randomUUID(), accountId, payeeId, reference,
                amount, account.getCurrency(), frequency, firstRun, at));
    }

    public void pause(UUID scheduledPaymentId) {
        scheduledPayment(scheduledPaymentId).setStatus(ScheduledPaymentStatus.PAUSED);
    }

    public void resume(UUID scheduledPaymentId) {
        scheduledPayment(scheduledPaymentId).setStatus(ScheduledPaymentStatus.ACTIVE);
    }

    public void cancel(UUID scheduledPaymentId) {
        scheduledPayment(scheduledPaymentId).setStatus(ScheduledPaymentStatus.CANCELLED);
    }

    /**
     * Executes the payment for its next run date, which must have arrived by {@code at}. A paused
     * payment is skipped. A recurring payment moves on to its next date whatever the result; a
     * one-off payment completes only when it succeeds.
     */
    public PaymentAttempt runDue(UUID scheduledPaymentId, Instant at) {
        ScheduledPayment sp = scheduledPayment(scheduledPaymentId);
        LocalDate runDate = sp.getNextRun();
        if (runDate.isAfter(AccountOperations.day(at))) {
            throw new DomainException("Scheduled payment " + sp.getId() + " is not due until " + runDate);
        }
        switch (sp.getStatus()) {
            case CANCELLED, COMPLETED -> throw new DomainException("Scheduled payment " + sp.getId() + " is " + sp.getStatus().wire());
            case PAUSED -> {
                sp.advanceAfter(runDate);
                return attempts.save(new PaymentAttempt(UUID.randomUUID(), sp.getId(), sp.getAmount(), runDate,
                        PaymentAttemptResult.SKIPPED, null, 0, at));
            }
            default -> {
            }
        }
        PaymentAttempt attempt = execute(sp, runDate, 0, at);
        if (attempt.getResult() == PaymentAttemptResult.SUCCEEDED || sp.getFrequency() != Frequency.ONCE) {
            sp.advanceAfter(runDate);
        }
        return attempt;
    }

    /** Retries the latest failed run of the payment (same run date, retry count + 1). */
    public PaymentAttempt retryFailedRun(UUID scheduledPaymentId, Instant at) {
        ScheduledPayment sp = scheduledPayment(scheduledPaymentId);
        PaymentAttempt last = attempts.findByScheduledPaymentIdInOrderByAttemptedAtDescRetryCountDescIdDesc(List.of(sp.getId()))
                .stream().findFirst()
                .orElseThrow(() -> new DomainException("Scheduled payment " + sp.getId() + " has not run yet"));
        if (last.getResult() != PaymentAttemptResult.FAILED) {
            throw new DomainException("The last run of " + sp.getId() + " did not fail");
        }
        if (last.getRetryCount() >= MAX_RETRIES) {
            throw new DomainException("No retries left for " + sp.getId());
        }
        PaymentAttempt attempt = execute(sp, last.getRunDate(), last.getRetryCount() + 1, at);
        if (attempt.getResult() == PaymentAttemptResult.SUCCEEDED && sp.getFrequency() == Frequency.ONCE) {
            sp.advanceAfter(last.getRunDate());
        }
        return attempt;
    }

    private PaymentAttempt execute(ScheduledPayment sp, LocalDate runDate, int retryCount, Instant at) {
        Account account = accounts.account(sp.getAccountId());
        Payee payee = payee(sp.getPayeeId());
        BigDecimal paidOutToday = accounts.paidOutOn(account.getId(), AccountOperations.day(at), at);
        Optional<PaymentFailureReason> failure = PaymentRules.check(account, payee, sp.getAmount(), paidOutToday);
        PaymentAttempt attempt = attempts.save(new PaymentAttempt(UUID.randomUUID(), sp.getId(), sp.getAmount(), runDate,
                failure.isPresent() ? PaymentAttemptResult.FAILED : PaymentAttemptResult.SUCCEEDED,
                failure.orElse(null), retryCount, at));
        if (failure.isEmpty()) {
            accounts.book(account, Money.normalise(sp.getAmount()).negate(), TransactionType.SCHEDULED_PAYMENT,
                    payee.getName() + " " + sp.getReference(), attempt.getId(), at);
        }
        return attempt;
    }

    public Payee payee(UUID payeeId) {
        return payees.findById(payeeId).orElseThrow(() -> new NotFoundException("Payee " + payeeId + " not found"));
    }

    public ScheduledPayment scheduledPayment(UUID id) {
        return scheduledPayments.findById(id)
                .orElseThrow(() -> new NotFoundException("Scheduled payment " + id + " not found"));
    }
}
