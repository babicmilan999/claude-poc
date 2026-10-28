package com.claudepoc.banking.domain;

import com.claudepoc.banking.domain.Enums.AccountStatus;
import jakarta.persistence.Entity;
import jakarta.persistence.EnumType;
import jakarta.persistence.Enumerated;
import jakarta.persistence.Id;
import jakarta.persistence.Table;
import java.math.BigDecimal;
import java.time.Instant;
import java.util.UUID;

@Entity
@Table(name = "account")
public class Account {

    @Id
    private UUID id;
    private UUID customerId;
    private String name;
    private String sortCode;
    private String accountNumber;
    private String currency;
    private BigDecimal balance;
    private BigDecimal overdraftLimit;
    private BigDecimal dailyPaymentLimit;
    @Enumerated(EnumType.STRING)
    private AccountStatus status;
    private Instant openedAt;

    protected Account() {
    }

    public Account(UUID id, UUID customerId, String name, String sortCode, String accountNumber, String currency,
                   BigDecimal overdraftLimit, BigDecimal dailyPaymentLimit, Instant openedAt) {
        this.id = id;
        this.customerId = customerId;
        this.name = name;
        this.sortCode = sortCode;
        this.accountNumber = accountNumber;
        this.currency = currency;
        this.balance = Money.ZERO;
        this.overdraftLimit = Money.normalise(overdraftLimit);
        this.dailyPaymentLimit = Money.normalise(dailyPaymentLimit);
        this.status = AccountStatus.ACTIVE;
        this.openedAt = openedAt;
    }

    public UUID getId() { return id; }
    public UUID getCustomerId() { return customerId; }
    public String getName() { return name; }
    public String getSortCode() { return sortCode; }
    public String getAccountNumber() { return accountNumber; }
    public String getCurrency() { return currency; }
    public BigDecimal getBalance() { return balance; }
    public BigDecimal getOverdraftLimit() { return overdraftLimit; }
    public BigDecimal getDailyPaymentLimit() { return dailyPaymentLimit; }
    public AccountStatus getStatus() { return status; }
    public Instant getOpenedAt() { return openedAt; }

    public boolean isActive() { return status == AccountStatus.ACTIVE; }

    /** Funds available to spend: balance plus overdraft (approved authorisations settle at once, so none are pending). */
    public BigDecimal available() { return balance.add(overdraftLimit); }

    public void applyAmount(BigDecimal signedAmount) { this.balance = Money.normalise(balance.add(signedAmount)); }
    public void setOverdraftLimit(BigDecimal overdraftLimit) { this.overdraftLimit = Money.normalise(overdraftLimit); }
    public void setDailyPaymentLimit(BigDecimal limit) { this.dailyPaymentLimit = Money.normalise(limit); }
    public void setStatus(AccountStatus status) { this.status = status; }
}
