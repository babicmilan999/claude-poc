package com.claudepoc.banking.domain;

import com.claudepoc.banking.domain.Enums.Frequency;
import com.claudepoc.banking.domain.Enums.ScheduledPaymentStatus;
import jakarta.persistence.Entity;
import jakarta.persistence.EnumType;
import jakarta.persistence.Enumerated;
import jakarta.persistence.Id;
import jakarta.persistence.Table;
import java.math.BigDecimal;
import java.time.Instant;
import java.time.LocalDate;
import java.util.UUID;

/** A standing order (weekly, monthly) or a one-off future payment to a payee. */
@Entity
@Table(name = "scheduled_payment")
public class ScheduledPayment {

    @Id
    private UUID id;
    private UUID accountId;
    private UUID payeeId;
    private String reference;
    private BigDecimal amount;
    private String currency;
    @Enumerated(EnumType.STRING)
    private Frequency frequency;
    private LocalDate nextRun;
    @Enumerated(EnumType.STRING)
    private ScheduledPaymentStatus status;
    private Instant createdAt;

    protected ScheduledPayment() {
    }

    public ScheduledPayment(UUID id, UUID accountId, UUID payeeId, String reference, BigDecimal amount,
                            String currency, Frequency frequency, LocalDate nextRun, Instant createdAt) {
        this.id = id;
        this.accountId = accountId;
        this.payeeId = payeeId;
        this.reference = reference;
        this.amount = Money.normalise(amount);
        this.currency = currency;
        this.frequency = frequency;
        this.nextRun = nextRun;
        this.status = ScheduledPaymentStatus.ACTIVE;
        this.createdAt = createdAt;
    }

    public UUID getId() { return id; }
    public UUID getAccountId() { return accountId; }
    public UUID getPayeeId() { return payeeId; }
    public String getReference() { return reference; }
    public BigDecimal getAmount() { return amount; }
    public String getCurrency() { return currency; }
    public Frequency getFrequency() { return frequency; }
    public LocalDate getNextRun() { return nextRun; }
    public ScheduledPaymentStatus getStatus() { return status; }
    public Instant getCreatedAt() { return createdAt; }

    public void setStatus(ScheduledPaymentStatus status) { this.status = status; }

    /** Moves the schedule past a run date; a one-off payment is completed by this. */
    public void advanceAfter(LocalDate runDate) {
        switch (frequency) {
            case ONCE -> status = ScheduledPaymentStatus.COMPLETED;
            case WEEKLY -> nextRun = runDate.plusWeeks(1);
            case MONTHLY -> nextRun = runDate.plusMonths(1);
        }
    }
}
