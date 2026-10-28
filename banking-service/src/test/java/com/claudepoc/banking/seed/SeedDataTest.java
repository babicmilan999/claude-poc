package com.claudepoc.banking.seed;

import static org.assertj.core.api.Assertions.assertThat;

import com.claudepoc.banking.api.BankingQueries;
import com.claudepoc.banking.domain.CardAuthorisation;
import com.claudepoc.banking.domain.DirectDebitCollection;
import com.claudepoc.banking.domain.Enums.AuthorisationResult;
import com.claudepoc.banking.domain.Enums.CollectionResult;
import com.claudepoc.banking.domain.Enums.PaymentAttemptResult;
import com.claudepoc.banking.domain.PaymentAttempt;
import java.util.EnumSet;
import java.util.List;
import java.util.Set;
import java.util.UUID;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.context.ActiveProfiles;

/** The seeded customers each show one problem, produced by the rules (and Alice and the vulnerable flag as set). */
@SpringBootTest
@ActiveProfiles("test")
class SeedDataTest {

    @Autowired BankingQueries queries;
    @Autowired SeedData seed;

    private List<String> declines(int n) {
        return queries.authorisations(SeedData.seedId(n), null, AuthorisationResult.DECLINED, null, 50).stream()
                .map(CardAuthorisation::getDeclineReason).map(Enum::name).toList();
    }

    private List<String> failedPayments(int n) {
        return queries.paymentAttempts(SeedData.seedId(n), null, Set.of(PaymentAttemptResult.FAILED), null, 50).stream()
                .map(PaymentAttempt::getFailureReason).map(Enum::name).distinct().toList();
    }

    private List<String> failedCollections(int n) {
        return queries.collections(SeedData.seedId(n), null, CollectionResult.FAILED, null, 50).stream()
                .map(BankingQueries.CollectionView::collection).map(DirectDebitCollection::getFailureCode).map(Enum::name).toList();
    }

    @Test
    void seedsThirteenCustomersWithFixedIds() {
        assertThat(queries.customers()).hasSize(seed.customers().size()).hasSize(13);
        assertThat(queries.customer(SeedData.seedId(1)).getName()).isEqualTo("Alice Morgan");
        assertThat(queries.customer(SeedData.seedId(7)).isVulnerable()).isTrue();
        assertThat(SeedData.seedId(13)).isEqualTo(UUID.fromString("00000000-0000-4000-8000-000000000013"));
    }

    @Test
    void aliceHasNoProblems() {
        assertThat(declines(1)).isEmpty();
        assertThat(failedPayments(1)).isEmpty();
        assertThat(failedCollections(1)).isEmpty();
        assertThat(queries.paymentAttempts(SeedData.seedId(1), null, EnumSet.of(PaymentAttemptResult.SUCCEEDED), null, 50)).hasSize(2);
    }

    @Test
    void eachSeededCustomerHasTheProblemTheRulesProduced() {
        assertThat(declines(2)).containsOnly("CARD_FROZEN");
        assertThat(failedPayments(3)).containsExactly("INSUFFICIENT_FUNDS");
        assertThat(failedCollections(4)).containsExactly("MANDATE_CANCELLED");
        assertThat(declines(5)).containsExactly("CARD_BLOCKED", "FRAUD_RULE_TRIGGERED");
        assertThat(queries.fraudFlags(SeedData.seedId(5), null, null)).hasSize(1);
        assertThat(declines(6)).containsExactly("CARD_EXPIRED");
        assertThat(declines(7)).containsExactly("INSUFFICIENT_FUNDS");
        assertThat(failedPayments(8)).containsExactly("PAYEE_ACCOUNT_CLOSED");
        assertThat(declines(9)).containsExactly("MERCHANT_CATEGORY_BLOCKED");
        assertThat(failedCollections(10)).containsExactly("ACCOUNT_CLOSED_BY_ORIGINATOR");
        assertThat(declines(11)).containsExactly("CONTACTLESS_LIMIT_EXCEEDED");
        assertThat(failedCollections(12)).containsExactly("INSUFFICIENT_FUNDS");
        assertThat(failedPayments(13)).containsExactly("DAILY_PAYMENT_LIMIT_EXCEEDED");
        for (int n = 2; n <= 13; n++) {
            assertThat(queries.collections(SeedData.seedId(n), null, CollectionResult.PENDING, null, 50)).isEmpty();
        }
    }
}
