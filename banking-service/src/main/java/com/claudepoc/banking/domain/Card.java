package com.claudepoc.banking.domain;

import com.claudepoc.banking.domain.Enums.CardBrand;
import com.claudepoc.banking.domain.Enums.CardStatus;
import com.claudepoc.banking.domain.Enums.CardStatusReason;
import jakarta.persistence.Convert;
import jakarta.persistence.Entity;
import jakarta.persistence.EnumType;
import jakarta.persistence.Enumerated;
import jakarta.persistence.Id;
import jakarta.persistence.Table;
import java.math.BigDecimal;
import java.time.Instant;
import java.time.LocalDate;
import java.util.ArrayList;
import java.util.List;
import java.util.UUID;

@Entity
@Table(name = "card")
public class Card {

    @Id
    private UUID id;
    private UUID accountId;
    private String last4;
    @Enumerated(EnumType.STRING)
    private CardBrand brand;
    @Enumerated(EnumType.STRING)
    private CardStatus status;
    @Enumerated(EnumType.STRING)
    private CardStatusReason statusReason;
    private LocalDate expiry;
    private BigDecimal contactlessLimit;
    @Convert(converter = StringListConverter.class)
    private List<String> blockedMerchantCategories = new ArrayList<>();
    private boolean onlinePaymentsEnabled;
    private Instant issuedAt;

    protected Card() {
    }

    public Card(UUID id, UUID accountId, String last4, CardBrand brand, LocalDate expiry,
                BigDecimal contactlessLimit, Instant issuedAt) {
        this.id = id;
        this.accountId = accountId;
        this.last4 = last4;
        this.brand = brand;
        this.status = CardStatus.ACTIVE;
        this.expiry = expiry;
        this.contactlessLimit = Money.normalise(contactlessLimit);
        this.onlinePaymentsEnabled = true;
        this.issuedAt = issuedAt;
    }

    public UUID getId() { return id; }
    public UUID getAccountId() { return accountId; }
    public String getLast4() { return last4; }
    public CardBrand getBrand() { return brand; }
    public CardStatus getStatus() { return status; }
    public CardStatusReason getStatusReason() { return statusReason; }
    public LocalDate getExpiry() { return expiry; }
    public BigDecimal getContactlessLimit() { return contactlessLimit; }
    public List<String> getBlockedMerchantCategories() { return List.copyOf(blockedMerchantCategories); }
    public boolean isOnlinePaymentsEnabled() { return onlinePaymentsEnabled; }
    public Instant getIssuedAt() { return issuedAt; }

    /** The expiry date is the last day the card is valid. */
    public boolean isPastExpiry(LocalDate day) { return day.isAfter(expiry); }

    public void changeStatus(CardStatus status, CardStatusReason reason) {
        this.status = status;
        this.statusReason = reason;
    }

    public void setContactlessLimit(BigDecimal limit) { this.contactlessLimit = Money.normalise(limit); }
    public void setBlockedMerchantCategories(List<String> mccs) { this.blockedMerchantCategories = new ArrayList<>(mccs); }
    public void setOnlinePaymentsEnabled(boolean enabled) { this.onlinePaymentsEnabled = enabled; }
}
