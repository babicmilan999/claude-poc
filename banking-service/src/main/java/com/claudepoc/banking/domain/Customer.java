package com.claudepoc.banking.domain;

import com.claudepoc.banking.domain.Enums.CustomerStatus;
import jakarta.persistence.Entity;
import jakarta.persistence.EnumType;
import jakarta.persistence.Enumerated;
import jakarta.persistence.Id;
import jakarta.persistence.Table;
import java.time.Instant;
import java.util.UUID;

@Entity
@Table(name = "customer")
public class Customer {

    @Id
    private UUID id;
    private String name;
    private String email;
    @Enumerated(EnumType.STRING)
    private CustomerStatus status;
    private boolean vulnerable;
    private Instant createdAt;

    protected Customer() {
    }

    public Customer(UUID id, String name, String email, boolean vulnerable, Instant createdAt) {
        this.id = id;
        this.name = name;
        this.email = email;
        this.status = CustomerStatus.ACTIVE;
        this.vulnerable = vulnerable;
        this.createdAt = createdAt;
    }

    public UUID getId() { return id; }
    public String getName() { return name; }
    public String getEmail() { return email; }
    public CustomerStatus getStatus() { return status; }
    public boolean isVulnerable() { return vulnerable; }
    public Instant getCreatedAt() { return createdAt; }

    public void setStatus(CustomerStatus status) { this.status = status; }
    public void setVulnerable(boolean vulnerable) { this.vulnerable = vulnerable; }
}
