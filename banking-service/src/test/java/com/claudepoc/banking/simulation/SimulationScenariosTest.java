package com.claudepoc.banking.simulation;

import static org.assertj.core.api.Assertions.assertThat;

import com.claudepoc.banking.contract.ContractClient;
import com.claudepoc.banking.contract.ContractClient.Response;
import com.claudepoc.banking.domain.Repositories.CustomerRepository;
import com.claudepoc.banking.seed.SeedData;
import com.claudepoc.banking.simulation.ScenarioCatalog.Scenario;
import com.claudepoc.banking.simulation.SimulationEnums.IssueType;
import com.claudepoc.banking.simulation.SimulationEnums.RootCause;
import com.fasterxml.jackson.databind.JsonNode;
import java.util.Map;
import java.util.stream.Stream;
import java.util.stream.StreamSupport;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.Arguments;
import org.junit.jupiter.params.provider.MethodSource;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.web.servlet.MockMvc;

/**
 * One test per scenario row of architecture section 5. Each creates an incident through
 * POST /simulation/incidents, checks the response against the contract and the answer key,
 * then reads the customer back through the read API to prove the rules produced the outcome.
 */
@SpringBootTest
@AutoConfigureMockMvc
@ActiveProfiles({"test", "simulation"})
class SimulationScenariosTest {

    @Autowired
    private MockMvc mvc;
    @Autowired
    private CustomerRepository customers;
    private ContractClient api;

    @BeforeEach
    void setUp() {
        api = new ContractClient(mvc);
    }

    static Stream<Arguments> sectionFiveRows() {
        return Stream.of(
                Arguments.of("card_decline", "card_frozen_by_customer", "self_serve"),
                Arguments.of("card_decline", "insufficient_funds", "self_serve"),
                Arguments.of("card_decline", "card_expired", "self_serve"),
                Arguments.of("card_decline", "contactless_limit_exceeded", "self_serve"),
                Arguments.of("card_decline", "merchant_category_blocked", "self_serve"),
                Arguments.of("card_decline", "fraud_rule_triggered", "needs_human"),
                Arguments.of("missed_payment", "insufficient_funds", "self_serve"),
                Arguments.of("missed_payment", "daily_payment_limit_exceeded", "self_serve"),
                Arguments.of("missed_payment", "payee_account_closed", "needs_human"),
                Arguments.of("failed_direct_debit", "mandate_cancelled_by_customer", "self_serve"),
                Arguments.of("failed_direct_debit", "insufficient_funds", "self_serve"),
                Arguments.of("failed_direct_debit", "account_closed_by_originator", "needs_human"));
    }

    @Test
    void theCatalogCoversExactlyTheSectionFiveRows() {
        assertThat(ScenarioCatalog.SCENARIOS).hasSize((int) sectionFiveRows().count());
        assertThat(ScenarioCatalog.SCENARIOS.stream().map(s -> s.type().wire() + "/" + s.rootCause().wire() + "/" + s.outcome().wire()))
                .containsExactlyInAnyOrderElementsOf(sectionFiveRows()
                        .map(a -> a.get()[0] + "/" + a.get()[1] + "/" + a.get()[2]).toList());
    }

    @ParameterizedTest(name = "{0} / {1} -> {2}")
    @MethodSource("sectionFiveRows")
    void scenarioIsCreatedThroughTheRules(String type, String rootCause, String outcome) throws Exception {
        long before = customers.count();
        Response r = api.post("/simulation/incidents", "{\"type\":\"" + type + "\",\"root_cause\":\"" + rootCause + "\"}");
        assertThat(r.status()).as(r.body()).isEqualTo(201);
        JsonNode incident = r.json();
        assertThat(incident.get("issue_type").asText()).isEqualTo(type);
        assertThat(incident.get("expected_root_cause").asText()).isEqualTo(rootCause);
        assertThat(incident.get("expected_outcome").asText()).isEqualTo(outcome);
        assertThat(customers.count()).as("a fresh test customer").isEqualTo(before + 1);
        assertThat(incident.get("persona_hints").get("knows")).isEqualTo(incident.get("customer_knowledge"));
        assertThat(incident.get("customer_knowledge")).isNotEmpty();
        assertThat(incident.get("complaint").asText()).isNotBlank();
        // What the customer knows never names the root cause or the bank's reason code.
        String knowledge = incident.get("customer_knowledge").toString() + incident.get("complaint").asText();
        assertThat(knowledge.toLowerCase()).doesNotContain(rootCause.replace('_', ' '), rootCause, "fraud");

        Map<String, Object> c = Map.of("customer_id", incident.get("customer_id").asText());
        JsonNode affected = incident.get("affected");
        switch (type) {
            case "card_decline" -> verifyCardDecline(rootCause, c, affected);
            case "missed_payment" -> verifyMissedPayment(rootCause, c, affected);
            case "failed_direct_debit" -> verifyDirectDebit(rootCause, c, affected);
            default -> throw new IllegalStateException(type);
        }
    }

    private void verifyCardDecline(String rootCause, Map<String, Object> c, JsonNode affected) throws Exception {
        String cardId = affected.get("card_id").asText();
        JsonNode declines = api.get("/customers/{customer_id}/authorisations", c, "result=declined&card_id=" + cardId).json();
        assertThat(declines).isNotEmpty();
        JsonNode card = find(api.get("/customers/{customer_id}/cards", c).json(), cardId);
        String reason = switch (rootCause) {
            case "card_frozen_by_customer" -> "CARD_FROZEN";
            case "insufficient_funds" -> "INSUFFICIENT_FUNDS";
            case "card_expired" -> "CARD_EXPIRED";
            case "contactless_limit_exceeded" -> "CONTACTLESS_LIMIT_EXCEEDED";
            case "merchant_category_blocked" -> "MERCHANT_CATEGORY_BLOCKED";
            case "fraud_rule_triggered" -> "FRAUD_RULE_TRIGGERED";
            default -> throw new IllegalStateException(rootCause);
        };
        assertThat(declines).anySatisfy(a -> assertThat(a.get("decline_reason").asText()).isEqualTo(reason));
        JsonNode controls = api.get("/customers/{customer_id}/cards/{card_id}/controls",
                Map.of("customer_id", c.get("customer_id"), "card_id", cardId)).json();
        switch (rootCause) {
            case "card_frozen_by_customer" -> {
                assertThat(card.get("status").asText()).isEqualTo("frozen");
                assertThat(card.get("status_reason").asText()).isEqualTo("FROZEN_BY_CUSTOMER");
                assertThat(declines).allSatisfy(a -> assertThat(a.get("decline_reason").asText()).isEqualTo("CARD_FROZEN"));
                assertThat(declines).hasSize(2);
            }
            case "insufficient_funds" -> {
                JsonNode balance = api.get("/customers/{customer_id}/accounts/{account_id}/balance",
                        Map.of("customer_id", c.get("customer_id"), "account_id", affected.get("account_id").asText())).json();
                assertThat(new java.math.BigDecimal(declines.get(0).get("amount").asText()))
                        .isGreaterThan(new java.math.BigDecimal(balance.get("available").asText()));
            }
            case "card_expired" -> {
                assertThat(card.get("status").asText()).isEqualTo("expired");
                assertThat(card.get("status_reason").asText()).isEqualTo("EXPIRED");
                assertThat(api.get("/customers/{customer_id}/authorisations", c, "result=approved&card_id=" + cardId).json())
                        .as("the card worked before it expired").isNotEmpty();
            }
            case "contactless_limit_exceeded" -> {
                assertThat(declines.get(0).get("channel").asText()).isEqualTo("contactless");
                assertThat(new java.math.BigDecimal(declines.get(0).get("amount").asText()))
                        .isGreaterThan(new java.math.BigDecimal(controls.get("contactless_limit").asText()));
            }
            case "merchant_category_blocked" ->
                    assertThat(controls.get("blocked_merchant_categories").toString()).contains(declines.get(0).get("merchant_mcc").asText());
            case "fraud_rule_triggered" -> {
                assertThat(card.get("status").asText()).isEqualTo("blocked");
                assertThat(card.get("status_reason").asText()).isEqualTo("FRAUD_SUSPECTED");
                JsonNode fraudDecline = declines.get(declines.size() - 1);
                assertThat(fraudDecline.get("decline_reason").asText()).isEqualTo("FRAUD_RULE_TRIGGERED");
                assertThat(fraudDecline.get("fraud_rule_id").asText()).isEqualTo("FR-12");
                assertThat(declines.get(0).get("decline_reason").asText()).isEqualTo("CARD_BLOCKED");
                JsonNode flags = api.get("/customers/{customer_id}/fraud-flags", c, "status=open").json();
                assertThat(flags).singleElement().satisfies(f -> {
                    assertThat(f.get("rule_id").asText()).isEqualTo("FR-12");
                    assertThat(f.get("card_id").asText()).isEqualTo(cardId);
                    assertThat(f.get("action").asText()).isEqualTo("card_blocked");
                });
            }
            default -> throw new IllegalStateException(rootCause);
        }
    }

    private void verifyMissedPayment(String rootCause, Map<String, Object> c, JsonNode affected) throws Exception {
        String spId = affected.get("scheduled_payment_id").asText();
        JsonNode sp = find(api.get("/customers/{customer_id}/scheduled-payments", c).json(), spId);
        JsonNode attempts = api.get("/customers/{customer_id}/payment-attempts", c, "scheduled_payment_id=" + spId).json();
        assertThat(attempts.get(attempts.size() - 1).get("result").asText()).as("earlier months paid").isEqualTo("succeeded");
        String reason = switch (rootCause) {
            case "insufficient_funds" -> "INSUFFICIENT_FUNDS";
            case "daily_payment_limit_exceeded" -> "DAILY_PAYMENT_LIMIT_EXCEEDED";
            case "payee_account_closed" -> "PAYEE_ACCOUNT_CLOSED";
            default -> throw new IllegalStateException(rootCause);
        };
        assertThat(sp.get("last_attempt").get("result").asText()).isEqualTo("failed");
        assertThat(sp.get("last_attempt").get("failure_reason").asText()).isEqualTo(reason);
        JsonNode failed = api.get("/customers/{customer_id}/payment-attempts", c, "result=failed,skipped").json();
        assertThat(failed).isNotEmpty().allSatisfy(a -> assertThat(a.get("failure_reason").asText()).isEqualTo(reason));
        switch (rootCause) {
            case "insufficient_funds" -> assertThat(sp.get("last_attempt").get("retry_count").asInt()).isEqualTo(1);
            case "daily_payment_limit_exceeded" -> {
                JsonNode tx = api.get("/customers/{customer_id}/accounts/{account_id}/transactions",
                        Map.of("customer_id", c.get("customer_id"), "account_id", affected.get("account_id").asText())).json();
                assertThat(tx).anySatisfy(t -> assertThat(t.get("type").asText()).isEqualTo("transfer_out"));
            }
            case "payee_account_closed" -> assertThat(api.get("/customers/{customer_id}/payees/{payee_id}",
                    Map.of("customer_id", c.get("customer_id"), "payee_id", sp.get("payee_id").asText()))
                    .json().get("account_status").asText()).isEqualTo("closed");
            default -> throw new IllegalStateException(rootCause);
        }
    }

    private void verifyDirectDebit(String rootCause, Map<String, Object> c, JsonNode affected) throws Exception {
        String mandateId = affected.get("mandate_id").asText();
        JsonNode mandate = find(api.get("/customers/{customer_id}/mandates", c).json(), mandateId);
        JsonNode collections = api.get("/customers/{customer_id}/direct-debit-collections", c, "mandate_id=" + mandateId).json();
        assertThat(collections).hasSize(3);
        assertThat(collections.get(1).get("result").asText()).isEqualTo("collected");
        assertThat(collections.get(2).get("result").asText()).isEqualTo("collected");
        JsonNode latest = collections.get(0);
        assertThat(latest.get("result").asText()).isEqualTo("failed");
        switch (rootCause) {
            case "mandate_cancelled_by_customer" -> {
                assertThat(latest.get("failure_code").asText()).isEqualTo("MANDATE_CANCELLED");
                assertThat(mandate.get("status").asText()).isEqualTo("cancelled");
                assertThat(mandate.get("cancelled_by").asText()).isEqualTo("customer");
            }
            case "insufficient_funds" -> {
                assertThat(latest.get("failure_code").asText()).isEqualTo("INSUFFICIENT_FUNDS");
                assertThat(mandate.get("status").asText()).isEqualTo("active");
            }
            case "account_closed_by_originator" -> {
                assertThat(latest.get("failure_code").asText()).isEqualTo("ACCOUNT_CLOSED_BY_ORIGINATOR");
                assertThat(mandate.get("cancelled_by").asText()).isEqualTo("originator");
            }
            default -> throw new IllegalStateException(rootCause);
        }
    }

    @Test
    void withoutABodyARandomScenarioIsCreated() throws Exception {
        Response r = api.post("/simulation/incidents", null);
        assertThat(r.status()).as(r.body()).isEqualTo(201);
        IssueType type = com.claudepoc.banking.domain.Enums.fromWire(IssueType.class, r.json().get("issue_type").asText());
        RootCause cause = com.claudepoc.banking.domain.Enums.fromWire(RootCause.class, r.json().get("expected_root_cause").asText());
        assertThat(ScenarioCatalog.find(type, cause)).isPresent();
    }

    @Test
    void onlyTheTypeChoosesARootCauseOfThatType() throws Exception {
        for (int i = 0; i < 3; i++) {
            Response r = api.post("/simulation/incidents", "{\"type\":\"failed_direct_debit\"}");
            assertThat(r.status()).isEqualTo(201);
            assertThat(r.json().get("issue_type").asText()).isEqualTo("failed_direct_debit");
            assertThat(ScenarioCatalog.ofType(IssueType.FAILED_DIRECT_DEBIT).stream().map(Scenario::rootCause).map(RootCause::wire))
                    .contains(r.json().get("expected_root_cause").asText());
        }
    }

    @Test
    void anExistingSeededCustomerCanBeUsed() throws Exception {
        long before = customers.count();
        String alice = SeedData.seedId(1).toString();
        Response r = api.post("/simulation/incidents",
                "{\"type\":\"card_decline\",\"root_cause\":\"card_frozen_by_customer\",\"customer_id\":\"" + alice
                        + "\",\"params\":{\"merchant_name\":\"Lidl\",\"declined_attempts\":3}}");
        assertThat(r.status()).as(r.body()).isEqualTo(201);
        assertThat(r.json().get("customer_id").asText()).isEqualTo(alice);
        assertThat(r.json().get("customer_name").asText()).isEqualTo("Alice Morgan");
        assertThat(customers.count()).isEqualTo(before);
        JsonNode declines = api.get("/customers/{customer_id}/authorisations", Map.of("customer_id", alice), "result=declined").json();
        assertThat(declines).hasSize(3).allSatisfy(a -> {
            assertThat(a.get("decline_reason").asText()).isEqualTo("CARD_FROZEN");
            assertThat(a.get("merchant_name").asText()).isEqualTo("Lidl");
        });
    }

    @Test
    void invalidRequestsAre400AndChangeNothing() throws Exception {
        long before = customers.count();
        for (String body : new String[] {
                "{\"type\":\"other\"}",
                "{\"type\":\"card_decline\",\"root_cause\":\"payee_account_closed\"}",
                "{\"type\":\"bogus\"}",
                "{\"root_cause\":\"online_payments_disabled\"}",
                "{\"customer_id\":\"" + java.util.UUID.randomUUID() + "\"}",
                "{\"customer_id\":\"not-a-uuid\"}",
                "{\"type\":\"card_decline\",\"root_cause\":\"insufficient_funds\",\"params\":{\"amount\":\"0.01\"}}",
                "{\"type\":\"card_decline\",\"root_cause\":\"card_frozen_by_customer\",\"params\":{\"declined_attempts\":99}}",
                "{\"type\":\"card_decline\",\"root_cause\":\"fraud_rule_triggered\",\"params\":{\"merchant_mcc\":\"5411\"}}"}) {
            Response r = api.post("/simulation/incidents", body);
            assertThat(r.status()).as(body + " -> " + r.body()).isEqualTo(400);
        }
        assertThat(customers.count()).as("failed set-ups roll back").isEqualTo(before);
    }

    @Test
    void aRootCauseWithoutTypeIsAccepted() throws Exception {
        Response r = api.post("/simulation/incidents", "{\"root_cause\":\"insufficient_funds\"}");
        assertThat(r.status()).isEqualTo(201);
        assertThat(r.json().get("expected_root_cause").asText()).isEqualTo("insufficient_funds");
    }

    private static JsonNode find(JsonNode array, String id) {
        return StreamSupport.stream(array.spliterator(), false)
                .filter(n -> n.get("id").asText().equals(id))
                .findFirst()
                .orElseThrow(() -> new AssertionError("No record " + id + " in " + array));
    }
}
