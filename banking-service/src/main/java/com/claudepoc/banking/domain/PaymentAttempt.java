package com.claudepoc.banking.domain;

import com.claudepoc.banking.domain.Enums.PaymentAttemptResult;
import com.claudepoc.banking.domain.Enums.PaymentFailureReason;
import jakarta.persistence.Entity;
import jakarta.persistence.EnumType;
import jakarta.persistence.Enumerated;
import jakarta.persistence.Id;
import jakarta.persistence.Table;
import java.math.BigDecimal;
import java.time.Instant;
import java.time.LocalDate;
import java.util.UUID;

/** One execution of a scheduled payment on a run date (retries share the run date). */
@Entity
@Table(name = "payment_attempt")
public class PaymentAttempt {

    @Id
    private UUID id;
    private UUID scheduledPaymentId;
    private BigDecimal amount;
    private LocalDate runDate;
    @Enumerated(EnumType.STRING)
    private PaymentAttemptResult result;
    @Enumerated(EnumType.STRING)
    private PaymentFailureReason failureReason;
    private int retryCount;
    private Instant attemptedAt;

    protected PaymentAttempt() {
    }

    public PaymentAttempt(UUID id, UUID scheduledPaymentId, BigDecimal amount, LocalDate runDate,
                          PaymentAttemptResult result, PaymentFailureReason failureReason, int retryCount,
                          Instant attemptedAt) {
        this.id = id;
        this.scheduledPaymentId = scheduledPaymentId;
        this.amount = Money.normalise(amount);
        this.runDate = runDate;
        this.result = result;
        this.failureReason = failureReason;
        this.retryCount = retryCount;
        this.attemptedAt = attemptedAt;
    }

    public UUID getId() { return id; }
    public UUID getScheduledPaymentId() { return scheduledPaymentId; }
    public BigDecimal getAmount() { return amount; }
    public LocalDate getRunDate() { return runDate; }
    public PaymentAttemptResult getResult() { return result; }
    public PaymentFailureReason getFailureReason() { return failureReason; }
    public int getRetryCount() { return retryCount; }
    public Instant getAttemptedAt() { return attemptedAt; }
}
