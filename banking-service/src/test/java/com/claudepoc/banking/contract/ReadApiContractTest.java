package com.claudepoc.banking.contract;

import static org.assertj.core.api.Assertions.assertThat;

import com.claudepoc.banking.contract.ContractClient.Response;
import com.claudepoc.banking.seed.SeedData;
import com.fasterxml.jackson.databind.JsonNode;
import java.time.Instant;
import java.time.temporal.ChronoUnit;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.web.servlet.MockMvc;

/**
 * Every read endpoint, called for every seeded customer (with and without filters), returns
 * what contracts/banking-api.yaml declares: status, content type and body schema.
 */
@SpringBootTest
@AutoConfigureMockMvc
@ActiveProfiles("test")
class ReadApiContractTest {

    private static final int SEEDED = 13;

    @Autowired
    private MockMvc mvc;
    private ContractClient api;
    private final List<String> bodies = new ArrayList<>();

    @BeforeEach
    void setUp() {
        api = new ContractClient(mvc);
    }

    @Test
    void everyReadOperationMatchesTheContractForEverySeededCustomer() throws Exception {
        Response customers = call("/customers", Map.of(), null);
        assertThat(customers.status()).isEqualTo(200);
        assertThat(customers.json()).hasSize(SEEDED);

        String since = Instant.now().minus(40, ChronoUnit.DAYS).toString();
        String until = Instant.now().plus(1, ChronoUnit.DAYS).toString();
        for (int n = 1; n <= SEEDED; n++) {
            UUID c = SeedData.seedId(n);
            Map<String, Object> cv = Map.of("customer_id", c);
            assertThat(call("/customers/{customer_id}", cv, null).json().get("id").asText()).isEqualTo(c.toString());

            for (JsonNode account : call("/customers/{customer_id}/accounts", cv, null).json()) {
                Map<String, Object> av = Map.of("customer_id", c, "account_id", account.get("id").asText());
                call("/customers/{customer_id}/accounts/{account_id}/balance", av, null);
                assertThat(call("/customers/{customer_id}/accounts/{account_id}/transactions", av, null).json()).isNotEmpty();
                call("/customers/{customer_id}/accounts/{account_id}/transactions", av,
                        "since=" + since + "&until=" + until + "&limit=5");
            }
            for (JsonNode card : call("/customers/{customer_id}/cards", cv, null).json()) {
                call("/customers/{customer_id}/cards/{card_id}/controls",
                        Map.of("customer_id", c, "card_id", card.get("id").asText()), null);
                call("/customers/{customer_id}/authorisations", cv, "card_id=" + card.get("id").asText());
                call("/customers/{customer_id}/fraud-flags", cv, "card_id=" + card.get("id").asText());
            }
            call("/customers/{customer_id}/authorisations", cv, null);
            call("/customers/{customer_id}/authorisations", cv, "result=declined&since=" + since + "&limit=10");
            call("/customers/{customer_id}/fraud-flags", cv, null);
            call("/customers/{customer_id}/fraud-flags", cv, "status=open");
            call("/customers/{customer_id}/scheduled-payments", cv, null);
            call("/customers/{customer_id}/payment-attempts", cv, null);
            call("/customers/{customer_id}/payment-attempts", cv, "result=failed,skipped&since=" + since + "&limit=20");
            for (JsonNode payee : call("/customers/{customer_id}/payees", cv, null).json()) {
                call("/customers/{customer_id}/payees/{payee_id}",
                        Map.of("customer_id", c, "payee_id", payee.get("id").asText()), null);
            }
            for (JsonNode mandate : call("/customers/{customer_id}/mandates", cv, null).json()) {
                call("/customers/{customer_id}/direct-debit-collections", cv, "mandate_id=" + mandate.get("id").asText());
            }
            call("/customers/{customer_id}/mandates", cv, "status=cancelled");
            call("/customers/{customer_id}/direct-debit-collections", cv, null);
            call("/customers/{customer_id}/direct-debit-collections", cv, "result=failed&since=" + since + "&limit=10");
        }

        ContractValidator contract = ContractValidator.get();
        List<String> readOperations = contract.operations().stream().filter(o -> o.startsWith("GET ")).toList();
        assertThat(readOperations).hasSize(15);
        for (String op : readOperations) {
            assertThat(contract.covered()).as("validated with a 200 response").contains(op + " 200");
        }
        assertThat(bodies).allSatisfy(b -> assertThat(b).doesNotContain("expected_root_cause", "expected_outcome"));
    }

    @Test
    void unknownIdsAndOtherCustomersRecordsAre404ProblemDetails() throws Exception {
        UUID alice = SeedData.seedId(1);
        UUID ben = SeedData.seedId(2);
        UUID unknown = UUID.randomUUID();
        String benAccount = api.get("/customers/{customer_id}/accounts", Map.of("customer_id", ben)).json().get(0).get("id").asText();
        String benCard = api.get("/customers/{customer_id}/cards", Map.of("customer_id", ben)).json().get(0).get("id").asText();

        List<Response> notFound = List.of(
                api.get("/customers/{customer_id}", Map.of("customer_id", unknown)),
                api.get("/customers/{customer_id}", Map.of("customer_id", "not-a-uuid")),
                api.get("/customers/{customer_id}/accounts", Map.of("customer_id", unknown)),
                api.get("/customers/{customer_id}/accounts/{account_id}/balance", Map.of("customer_id", alice, "account_id", benAccount)),
                api.get("/customers/{customer_id}/accounts/{account_id}/transactions", Map.of("customer_id", alice, "account_id", benAccount)),
                api.get("/customers/{customer_id}/cards", Map.of("customer_id", unknown)),
                api.get("/customers/{customer_id}/cards/{card_id}/controls", Map.of("customer_id", alice, "card_id", benCard)),
                api.get("/customers/{customer_id}/authorisations", Map.of("customer_id", alice), "card_id=" + benCard),
                api.get("/customers/{customer_id}/fraud-flags", Map.of("customer_id", unknown)),
                api.get("/customers/{customer_id}/scheduled-payments", Map.of("customer_id", unknown)),
                api.get("/customers/{customer_id}/payment-attempts", Map.of("customer_id", alice), "scheduled_payment_id=" + unknown),
                api.get("/customers/{customer_id}/payees", Map.of("customer_id", unknown)),
                api.get("/customers/{customer_id}/payees/{payee_id}", Map.of("customer_id", alice, "payee_id", unknown)),
                api.get("/customers/{customer_id}/mandates", Map.of("customer_id", unknown)),
                api.get("/customers/{customer_id}/direct-debit-collections", Map.of("customer_id", alice), "mandate_id=" + unknown));
        assertThat(notFound).allSatisfy(r -> {
            assertThat(r.status()).isEqualTo(404);
            assertThat(r.contentType()).startsWith("application/problem+json");
        });
    }

    @Test
    void simulationEndpointIs404WithoutTheSimulationProfile() throws Exception {
        Response r = api.post("/simulation/incidents", "{\"type\":\"card_decline\"}");
        assertThat(r.status()).isEqualTo(404);
        assertThat(r.contentType()).startsWith("application/problem+json");
    }

    @Test
    void invalidQueryValuesAre400ProblemDetails() throws Exception {
        // The contract declares no 400 on these operations (reported as a contract gap), so the
        // body is checked against the Problem schema directly.
        UUID ben = SeedData.seedId(2);
        for (String query : List.of("limit=0", "limit=201", "result=maybe", "since=yesterday")) {
            var result = mvc.perform(org.springframework.test.web.servlet.request.MockMvcRequestBuilders
                    .get("/customers/" + ben + "/authorisations?" + query)).andReturn().getResponse();
            assertThat(result.getStatus()).as(query).isEqualTo(400);
            assertThat(result.getContentType()).startsWith("application/problem+json");
            ContractValidator.get().assertMatchesSchema("Problem", result.getContentAsString());
        }
    }

    @Test
    void listsAreNewestFirstAndHonourLimitAndFilters() throws Exception {
        Map<String, Object> ben = Map.of("customer_id", SeedData.seedId(2));
        JsonNode all = api.get("/customers/{customer_id}/authorisations", ben).json();
        for (int i = 1; i < all.size(); i++) {
            assertThat(Instant.parse(all.get(i - 1).get("created_at").asText()))
                    .isAfterOrEqualTo(Instant.parse(all.get(i).get("created_at").asText()));
        }
        assertThat(api.get("/customers/{customer_id}/authorisations", ben, "limit=1").json()).hasSize(1);
        JsonNode declined = api.get("/customers/{customer_id}/authorisations", ben, "result=declined").json();
        assertThat(declined).isNotEmpty().allSatisfy(a -> assertThat(a.get("result").asText()).isEqualTo("declined"));

        Map<String, Object> chloe = Map.of("customer_id", SeedData.seedId(3));
        JsonNode attempts = api.get("/customers/{customer_id}/payment-attempts", chloe, "result=failed,skipped").json();
        assertThat(attempts).isNotEmpty().allSatisfy(a -> assertThat(a.get("result").asText()).isIn("failed", "skipped"));
        JsonNode succeeded = api.get("/customers/{customer_id}/payment-attempts", chloe, "result=succeeded").json();
        assertThat(succeeded).hasSize(2);
    }

    private Response call(String template, Map<String, Object> vars, String query) throws Exception {
        Response r = api.get(template, vars, query);
        assertThat(r.status()).as("GET %s %s", ContractClient.expand(template, vars), query).isEqualTo(200);
        bodies.add(r.body());
        return r;
    }
}
