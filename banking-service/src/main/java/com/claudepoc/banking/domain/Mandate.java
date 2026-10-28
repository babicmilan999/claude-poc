package com.claudepoc.banking.domain;

import com.claudepoc.banking.domain.Enums.CancelledBy;
import com.claudepoc.banking.domain.Enums.MandateStatus;
import jakarta.persistence.Entity;
import jakarta.persistence.EnumType;
import jakarta.persistence.Enumerated;
import jakarta.persistence.Id;
import jakarta.persistence.Table;
import java.time.Instant;
import java.util.UUID;

/** A direct debit mandate: the customer authorises an originator to collect from the account. */
@Entity
@Table(name = "dd_mandate")
public class Mandate {

    @Id
    private UUID id;
    private UUID accountId;
    private String originator;
    private String reference;
    @Enumerated(EnumType.STRING)
    private MandateStatus status;
    @Enumerated(EnumType.STRING)
    private CancelledBy cancelledBy;
    private Instant createdAt;
    private Instant cancelledAt;

    protected Mandate() {
    }

    public Mandate(UUID id, UUID accountId, String originator, String reference, Instant createdAt) {
        this.id = id;
        this.accountId = accountId;
        this.originator = originator;
        this.reference = reference;
        this.status = MandateStatus.ACTIVE;
        this.createdAt = createdAt;
    }

    public UUID getId() { return id; }
    public UUID getAccountId() { return accountId; }
    public String getOriginator() { return originator; }
    public String getReference() { return reference; }
    public MandateStatus getStatus() { return status; }
    public CancelledBy getCancelledBy() { return cancelledBy; }
    public Instant getCreatedAt() { return createdAt; }
    public Instant getCancelledAt() { return cancelledAt; }

    public void cancel(CancelledBy by, Instant at) {
        this.status = MandateStatus.CANCELLED;
        this.cancelledBy = by;
        this.cancelledAt = at;
    }

    public void markDormant() { this.status = MandateStatus.DORMANT; }
}
