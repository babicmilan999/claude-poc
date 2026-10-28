package com.claudepoc.banking.domain;

import com.claudepoc.banking.domain.Enums.PayeeAccountStatus;
import jakarta.persistence.Entity;
import jakarta.persistence.EnumType;
import jakarta.persistence.Enumerated;
import jakarta.persistence.Id;
import jakarta.persistence.Table;
import java.util.UUID;

@Entity
@Table(name = "payee")
public class Payee {

    @Id
    private UUID id;
    private UUID customerId;
    private String name;
    private String sortCode;
    private String accountNumber;
    @Enumerated(EnumType.STRING)
    private PayeeAccountStatus accountStatus;

    protected Payee() {
    }

    public Payee(UUID id, UUID customerId, String name, String sortCode, String accountNumber,
                 PayeeAccountStatus accountStatus) {
        this.id = id;
        this.customerId = customerId;
        this.name = name;
        this.sortCode = sortCode;
        this.accountNumber = accountNumber;
        this.accountStatus = accountStatus;
    }

    public UUID getId() { return id; }
    public UUID getCustomerId() { return customerId; }
    public String getName() { return name; }
    public String getSortCode() { return sortCode; }
    public String getAccountNumber() { return accountNumber; }
    public PayeeAccountStatus getAccountStatus() { return accountStatus; }

    public void setAccountStatus(PayeeAccountStatus accountStatus) { this.accountStatus = accountStatus; }
}
