package com.claudepoc.banking.domain;

import com.claudepoc.banking.domain.Enums.AuthorisationResult;
import com.claudepoc.banking.domain.Enums.Channel;
import com.claudepoc.banking.domain.Enums.DeclineReason;
import jakarta.persistence.Entity;
import jakarta.persistence.EnumType;
import jakarta.persistence.Enumerated;
import jakarta.persistence.Id;
import jakarta.persistence.Table;
import java.math.BigDecimal;
import java.time.Instant;
import java.util.UUID;

@Entity
@Table(name = "card_authorisation")
public class CardAuthorisation {

    @Id
    private UUID id;
    private UUID cardId;
    private BigDecimal amount;
    private String currency;
    private String merchantName;
    private String merchantMcc;
    @Enumerated(EnumType.STRING)
    private Channel channel;
    @Enumerated(EnumType.STRING)
    private AuthorisationResult result;
    @Enumerated(EnumType.STRING)
    private DeclineReason declineReason;
    private String fraudRuleId;
    private Instant createdAt;

    protected CardAuthorisation() {
    }

    public CardAuthorisation(UUID id, UUID cardId, BigDecimal amount, String currency, String merchantName,
                             String merchantMcc, Channel channel, AuthorisationResult result,
                             DeclineReason declineReason, String fraudRuleId, Instant createdAt) {
        this.id = id;
        this.cardId = cardId;
        this.amount = Money.normalise(amount);
        this.currency = currency;
        this.merchantName = merchantName;
        this.merchantMcc = merchantMcc;
        this.channel = channel;
        this.result = result;
        this.declineReason = declineReason;
        this.fraudRuleId = fraudRuleId;
        this.createdAt = createdAt;
    }

    public UUID getId() { return id; }
    public UUID getCardId() { return cardId; }
    public BigDecimal getAmount() { return amount; }
    public String getCurrency() { return currency; }
    public String getMerchantName() { return merchantName; }
    public String getMerchantMcc() { return merchantMcc; }
    public Channel getChannel() { return channel; }
    public AuthorisationResult getResult() { return result; }
    public DeclineReason getDeclineReason() { return declineReason; }
    public String getFraudRuleId() { return fraudRuleId; }
    public Instant getCreatedAt() { return createdAt; }
}
