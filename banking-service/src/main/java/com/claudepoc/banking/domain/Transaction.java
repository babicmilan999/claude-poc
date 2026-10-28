package com.claudepoc.banking.domain;

import com.claudepoc.banking.domain.Enums.TransactionType;
import jakarta.persistence.Entity;
import jakarta.persistence.EnumType;
import jakarta.persistence.Enumerated;
import jakarta.persistence.Id;
import jakarta.persistence.Table;
import java.math.BigDecimal;
import java.time.Instant;
import java.util.UUID;

/** A booked movement on an account. Negative amounts are money out. */
@Entity
@Table(name = "account_transaction")
public class Transaction {

    @Id
    private UUID id;
    private UUID accountId;
    private BigDecimal amount;
    private String currency;
    @Enumerated(EnumType.STRING)
    private TransactionType type;
    private String description;
    private UUID referenceId;
    private Instant bookedAt;

    protected Transaction() {
    }

    public Transaction(UUID id, UUID accountId, BigDecimal amount, String currency, TransactionType type,
                       String description, UUID referenceId, Instant bookedAt) {
        this.id = id;
        this.accountId = accountId;
        this.amount = Money.normalise(amount);
        this.currency = currency;
        this.type = type;
        this.description = description;
        this.referenceId = referenceId;
        this.bookedAt = bookedAt;
    }

    public UUID getId() { return id; }
    public UUID getAccountId() { return accountId; }
    public BigDecimal getAmount() { return amount; }
    public String getCurrency() { return currency; }
    public TransactionType getType() { return type; }
    public String getDescription() { return description; }
    public UUID getReferenceId() { return referenceId; }
    public Instant getBookedAt() { return bookedAt; }
}
