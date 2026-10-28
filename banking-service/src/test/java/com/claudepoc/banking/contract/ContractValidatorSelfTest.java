package com.claudepoc.banking.contract;

import static org.assertj.core.api.Assertions.assertThatCode;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import org.junit.jupiter.api.Test;

/** The validator must actually reject what the contract forbids, or the contract tests prove nothing. */
class ContractValidatorSelfTest {

    private final ContractValidator contract = ContractValidator.get();
    private static final String CARD = """
            {"id":"6f1c1e7e-3d2a-4c55-9a7e-0d3c1e2b4a10","account_id":"6f1c1e7e-3d2a-4c55-9a7e-0d3c1e2b4a11",
             "last4":"1234","brand":"visa","status":"frozen","status_reason":"FROZEN_BY_CUSTOMER","expiry":"2028-10-31"}""";

    @Test
    void acceptsAValidBody() {
        assertThatCode(() -> contract.assertResponse("GET", "/customers/{customer_id}/cards", 200,
                "application/json", "[" + CARD + "]")).doesNotThrowAnyException();
    }

    @Test
    void rejectsWrongEnumsFormatsMissingAndExtraFields() {
        assertThatThrownBy(() -> contract.assertMatchesSchema("Card", CARD.replace("\"frozen\"", "\"FROZEN\"")))
                .isInstanceOf(AssertionError.class);
        assertThatThrownBy(() -> contract.assertMatchesSchema("Card", CARD.replace("2028-10-31", "31/10/2028")))
                .isInstanceOf(AssertionError.class);
        assertThatThrownBy(() -> contract.assertMatchesSchema("Card", CARD.replace("\"last4\":\"1234\",", "")))
                .isInstanceOf(AssertionError.class);
        assertThatThrownBy(() -> contract.assertMatchesSchema("Card", CARD.replace("{\"id\"", "{\"expected_outcome\":\"self_serve\",\"id\"")))
                .isInstanceOf(AssertionError.class);
        assertThatThrownBy(() -> contract.assertMatchesSchema("Balance", """
                {"account_id":"6f1c1e7e-3d2a-4c55-9a7e-0d3c1e2b4a11","currency":"GBP","current":"12.5",
                 "available":"12.50","overdraft_limit":"0.00","as_of":"2026-10-28T10:00:00Z"}"""))
                .isInstanceOf(AssertionError.class);
    }

    @Test
    void rejectsUndeclaredStatusesAndContentTypes() {
        assertThatThrownBy(() -> contract.assertResponse("GET", "/customers/{customer_id}/cards", 500,
                "application/json", "{}")).isInstanceOf(AssertionError.class);
        assertThatThrownBy(() -> contract.assertResponse("GET", "/customers/{customer_id}", 404,
                "application/json", "{\"title\":\"Not found\",\"status\":404}")).isInstanceOf(AssertionError.class);
    }

    @Test
    void resolvesTheSharedScenarioEnums() {
        assertThatThrownBy(() -> contract.assertResponse("POST", "/simulation/incidents", 201, "application/json", """
                {"incident_id":"6f1c1e7e-3d2a-4c55-9a7e-0d3c1e2b4a10","issue_type":"card_decline",
                 "customer_id":"6f1c1e7e-3d2a-4c55-9a7e-0d3c1e2b4a11","customer_name":"A","persona_hints":{},
                 "customer_knowledge":[],"expected_root_cause":"not_a_cause","expected_outcome":"self_serve",
                 "created_at":"2026-10-28T10:00:00Z"}""")).isInstanceOf(AssertionError.class);
    }
}
