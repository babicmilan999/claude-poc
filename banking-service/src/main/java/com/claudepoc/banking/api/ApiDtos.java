package com.claudepoc.banking.api;

import java.math.BigDecimal;
import java.util.List;
import java.util.UUID;

/**
 * Response bodies of the read API, one record per schema in contracts/banking-api.yaml.
 * Field names become snake_case through the global Jackson naming strategy. Money is a string
 * with two decimals; timestamps are Instants (RFC 3339 UTC); dates are ISO dates.
 */
public final class ApiDtos {

    private ApiDtos() {
    }

    public record CustomerDto(UUID id, String name, String email, String status, boolean vulnerable,
                              java.time.Instant createdAt) {
    }

    public record AccountDto(UUID id, UUID customerId, String name, String sortCode, String accountNumber,
                             String currency, String balance, String overdraftLimit, String status) {
    }

    public record BalanceDto(UUID accountId, String currency, String current, String available, String overdraftLimit,
                             java.time.Instant asOf) {
    }

    public record TransactionDto(UUID id, UUID accountId, String amount, String currency, String type,
                                 String description, UUID referenceId, java.time.Instant bookedAt) {
    }

    public record CardDto(UUID id, UUID accountId, String last4, String brand, String status, String statusReason,
                          java.time.LocalDate expiry) {
    }

    public record CardControlsDto(UUID cardId, String contactlessLimit, List<String> blockedMerchantCategories,
                                  boolean onlinePaymentsEnabled) {
    }

    public record CardAuthorisationDto(UUID id, UUID cardId, String amount, String currency, String merchantName,
                                       String merchantMcc, String channel, String result, String declineReason,
                                       String fraudRuleId, java.time.Instant createdAt) {
    }

    public record FraudFlagDto(UUID id, UUID customerId, UUID cardId, String ruleId, String description,
                               BigDecimal riskScore, String status, String action, java.time.Instant createdAt,
                               java.time.Instant clearedAt) {
    }

    public record ScheduledPaymentDto(UUID id, UUID accountId, UUID payeeId, String payeeName, String reference,
                                      String amount, String currency, String frequency, java.time.LocalDate nextRun,
                                      String status, PaymentAttemptDto lastAttempt) {
    }

    public record PaymentAttemptDto(UUID id, UUID scheduledPaymentId, String amount, String result,
                                    String failureReason, int retryCount, java.time.Instant attemptedAt) {
    }

    public record PayeeDto(UUID id, UUID customerId, String name, String sortCode, String accountNumber,
                           String accountStatus) {
    }

    public record MandateDto(UUID id, UUID accountId, String originator, String reference, String status,
                             String cancelledBy, java.time.Instant createdAt, java.time.Instant cancelledAt) {
    }

    public record DirectDebitCollectionDto(UUID id, UUID mandateId, String originator, String amount, String currency,
                                           java.time.LocalDate dueDate, String result, String failureCode,
                                           java.time.Instant processedAt) {
    }
}
