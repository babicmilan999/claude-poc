package com.claudepoc.banking.domain;

import com.claudepoc.banking.domain.Enums.FraudAction;
import com.claudepoc.banking.domain.Enums.FraudFlagStatus;
import jakarta.persistence.Entity;
import jakarta.persistence.EnumType;
import jakarta.persistence.Enumerated;
import jakarta.persistence.Id;
import jakarta.persistence.Table;
import java.math.BigDecimal;
import java.time.Instant;
import java.util.UUID;

@Entity
@Table(name = "fraud_flag")
public class FraudFlag {

    @Id
    private UUID id;
    private UUID customerId;
    private UUID cardId;
    private String ruleId;
    private String description;
    private BigDecimal riskScore;
    @Enumerated(EnumType.STRING)
    private FraudFlagStatus status;
    @Enumerated(EnumType.STRING)
    private FraudAction action;
    private Instant createdAt;
    private Instant clearedAt;

    protected FraudFlag() {
    }

    public FraudFlag(UUID id, UUID customerId, UUID cardId, String ruleId, String description,
                     BigDecimal riskScore, FraudAction action, Instant createdAt) {
        this.id = id;
        this.customerId = customerId;
        this.cardId = cardId;
        this.ruleId = ruleId;
        this.description = description;
        this.riskScore = riskScore;
        this.status = FraudFlagStatus.OPEN;
        this.action = action;
        this.createdAt = createdAt;
    }

    public UUID getId() { return id; }
    public UUID getCustomerId() { return customerId; }
    public UUID getCardId() { return cardId; }
    public String getRuleId() { return ruleId; }
    public String getDescription() { return description; }
    public BigDecimal getRiskScore() { return riskScore; }
    public FraudFlagStatus getStatus() { return status; }
    public FraudAction getAction() { return action; }
    public Instant getCreatedAt() { return createdAt; }
    public Instant getClearedAt() { return clearedAt; }

    public void resolve(FraudFlagStatus status, Instant at) {
        this.status = status;
        this.clearedAt = status == FraudFlagStatus.CLEARED ? at : null;
    }
}
