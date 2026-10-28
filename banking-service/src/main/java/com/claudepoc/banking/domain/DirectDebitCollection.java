package com.claudepoc.banking.domain;

import com.claudepoc.banking.domain.Enums.CollectionFailureCode;
import com.claudepoc.banking.domain.Enums.CollectionResult;
import jakarta.persistence.Entity;
import jakarta.persistence.EnumType;
import jakarta.persistence.Enumerated;
import jakarta.persistence.Id;
import jakarta.persistence.Table;
import java.math.BigDecimal;
import java.time.Instant;
import java.time.LocalDate;
import java.util.UUID;

/** A collection an originator presented against a mandate; pending until processed on the due date. */
@Entity
@Table(name = "dd_collection")
public class DirectDebitCollection {

    @Id
    private UUID id;
    private UUID mandateId;
    private BigDecimal amount;
    private String currency;
    private LocalDate dueDate;
    @Enumerated(EnumType.STRING)
    private CollectionResult result;
    @Enumerated(EnumType.STRING)
    private CollectionFailureCode failureCode;
    private Instant presentedAt;
    private Instant processedAt;

    protected DirectDebitCollection() {
    }

    public DirectDebitCollection(UUID id, UUID mandateId, BigDecimal amount, String currency, LocalDate dueDate,
                                 Instant presentedAt) {
        this.id = id;
        this.mandateId = mandateId;
        this.amount = Money.normalise(amount);
        this.currency = currency;
        this.dueDate = dueDate;
        this.result = CollectionResult.PENDING;
        this.presentedAt = presentedAt;
    }

    public UUID getId() { return id; }
    public UUID getMandateId() { return mandateId; }
    public BigDecimal getAmount() { return amount; }
    public String getCurrency() { return currency; }
    public LocalDate getDueDate() { return dueDate; }
    public CollectionResult getResult() { return result; }
    public CollectionFailureCode getFailureCode() { return failureCode; }
    public Instant getPresentedAt() { return presentedAt; }
    public Instant getProcessedAt() { return processedAt; }

    public void collected(Instant at) {
        this.result = CollectionResult.COLLECTED;
        this.failureCode = null;
        this.processedAt = at;
    }

    public void failed(CollectionFailureCode code, Instant at) {
        this.result = CollectionResult.FAILED;
        this.failureCode = code;
        this.processedAt = at;
    }
}
