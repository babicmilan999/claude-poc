package com.claudepoc.banking.situations;

import java.util.Map;
import java.util.UUID;

/**
 * What a situation touched, and plain facts about it (merchant, amount, payee...) that the
 * simulation module uses to write what the customer would know.
 */
public record SituationResult(UUID accountId, UUID cardId, UUID scheduledPaymentId, UUID mandateId,
                              Map<String, String> facts) {

    public String fact(String name) {
        return facts.getOrDefault(name, "");
    }
}
