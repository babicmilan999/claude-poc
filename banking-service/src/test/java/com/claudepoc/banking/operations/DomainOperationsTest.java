package com.claudepoc.banking.operations;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.claudepoc.banking.domain.Account;
import com.claudepoc.banking.domain.Card;
import com.claudepoc.banking.domain.CardAuthorisation;
import com.claudepoc.banking.domain.DomainException;
import com.claudepoc.banking.domain.Enums.AuthorisationResult;
import com.claudepoc.banking.domain.Enums.CardBrand;
import com.claudepoc.banking.domain.Enums.CardStatus;
import com.claudepoc.banking.domain.Enums.Channel;
import com.claudepoc.banking.domain.Enums.DeclineReason;
import com.claudepoc.banking.domain.Enums.Frequency;
import com.claudepoc.banking.domain.Enums.PaymentAttemptResult;
import com.claudepoc.banking.domain.Enums.PaymentFailureReason;
import com.claudepoc.banking.domain.Enums.ScheduledPaymentStatus;
import com.claudepoc.banking.domain.FraudFlag;
import com.claudepoc.banking.domain.Repositories.FraudFlagRepository;
import com.claudepoc.banking.domain.ScheduledPayment;
import com.claudepoc.banking.operations.AuthorisationOperations.AuthorisationRequest;
import java.math.BigDecimal;
import java.time.Instant;
import java.time.LocalDate;
import java.time.ZoneOffset;
import java.time.temporal.ChronoUnit;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.transaction.annotation.Transactional;

/** Domain operations against H2: what the rules do to cards, balances and schedules. */
@SpringBootTest
@ActiveProfiles("test")
@Transactional
class DomainOperationsTest {

    @Autowired AccountOperations accounts;
    @Autowired CardOperations cards;
    @Autowired AuthorisationOperations authorisations;
    @Autowired PaymentOperations payments;
    @Autowired FraudOperations fraud;
    @Autowired FraudFlagRepository fraudFlags;

    private final Instant now = Instant.now().truncatedTo(ChronoUnit.MICROS);
    private final LocalDate today = LocalDate.ofInstant(now, ZoneOffset.UTC);
    private Account account;
    private Card card;

    @BeforeEach
    void setUp() {
        var customer = accounts.onboardCustomer("Test Person", "test.person@example.com", false, now.minus(10, ChronoUnit.DAYS));
        account = accounts.openAccount(customer.getId(), "Current account", BigDecimal.ZERO, now.minus(10, ChronoUnit.DAYS));
        accounts.receiveTransfer(account.getId(), new BigDecimal("1000.00"), "Salary", now.minus(9, ChronoUnit.DAYS));
        card = cards.issueCard(account.getId(), CardBrand.VISA, today.plusYears(3), now.minus(9, ChronoUnit.DAYS));
    }

    private CardAuthorisation pay(String amount, String mcc, Channel channel, Instant at) {
        return authorisations.authorise(new AuthorisationRequest(card.getId(), new BigDecimal(amount), "Shop", mcc, channel, at));
    }

    @Test
    void anApprovedPaymentIsBookedAndADeclineIsNot() {
        assertThat(pay("40.00", "5411", Channel.CHIP_AND_PIN, now.minus(2, ChronoUnit.HOURS)).getResult()).isEqualTo(AuthorisationResult.APPROVED);
        assertThat(accounts.account(account.getId()).getBalance()).isEqualByComparingTo("960.00");
        CardAuthorisation declined = pay("961.00", "5411", Channel.CHIP_AND_PIN, now.minus(1, ChronoUnit.HOURS));
        assertThat(declined.getDeclineReason()).isEqualTo(DeclineReason.INSUFFICIENT_FUNDS);
        assertThat(accounts.account(account.getId()).getBalance()).isEqualByComparingTo("960.00");
    }

    @Test
    void aFraudHitBlocksTheCardAndClearingTheFlagUnblocksIt() {
        CardAuthorisation hit = pay("600.00", "6051", Channel.ONLINE, now.minus(1, ChronoUnit.HOURS));
        assertThat(hit.getDeclineReason()).isEqualTo(DeclineReason.FRAUD_RULE_TRIGGERED);
        assertThat(hit.getFraudRuleId()).isEqualTo("FR-12");
        assertThat(cards.card(card.getId()).getStatus()).isEqualTo(CardStatus.BLOCKED);
        assertThat(pay("5.00", "5411", Channel.CONTACTLESS, now.minus(30, ChronoUnit.MINUTES)).getDeclineReason())
                .isEqualTo(DeclineReason.CARD_BLOCKED);

        FraudFlag flag = fraudFlags.findByCustomerIdOrderByCreatedAtDescIdDesc(account.getCustomerId()).get(0);
        fraud.clear(flag.getId(), now);
        assertThat(cards.card(card.getId()).getStatus()).isEqualTo(CardStatus.ACTIVE);
        assertThat(pay("5.00", "5411", Channel.CONTACTLESS, now).getResult()).isEqualTo(AuthorisationResult.APPROVED);
    }

    @Test
    void velocityRuleFiresOnTheSixthPaymentInTenMinutes() {
        for (int i = 0; i < 5; i++) {
            assertThat(pay("2.00", "5814", Channel.CONTACTLESS, now.minus(9 - i, ChronoUnit.MINUTES)).getResult())
                    .isEqualTo(AuthorisationResult.APPROVED);
        }
        CardAuthorisation sixth = pay("2.00", "5814", Channel.CONTACTLESS, now.minus(3, ChronoUnit.MINUTES));
        assertThat(sixth.getFraudRuleId()).isEqualTo("FR-07");
    }

    @Test
    void unfreezingRestoresPayments() {
        cards.freezeByCustomer(card.getId());
        assertThat(pay("5.00", "5411", Channel.CHIP_AND_PIN, now.minus(1, ChronoUnit.HOURS)).getDeclineReason()).isEqualTo(DeclineReason.CARD_FROZEN);
        cards.unfreeze(card.getId());
        assertThat(pay("5.00", "5411", Channel.CHIP_AND_PIN, now).getResult()).isEqualTo(AuthorisationResult.APPROVED);
        assertThatThrownBy(() -> cards.unfreeze(card.getId())).isInstanceOf(DomainException.class);
    }

    @Test
    void transfersCountTowardsTheDailyLimitAndARejectedTransferIsNotBooked() {
        accounts.setDailyPaymentLimit(account.getId(), new BigDecimal("300.00"));
        Instant early = LocalDate.ofInstant(now, ZoneOffset.UTC).atStartOfDay(ZoneOffset.UTC).toInstant();
        assertThat(accounts.sendTransfer(account.getId(), new BigDecimal("200.00"), "Savings", early)).isEmpty();
        assertThat(accounts.sendTransfer(account.getId(), new BigDecimal("150.00"), "Savings", now))
                .contains(PaymentFailureReason.DAILY_PAYMENT_LIMIT_EXCEEDED);
        assertThat(accounts.account(account.getId()).getBalance()).isEqualByComparingTo("800.00");
    }

    @Test
    void aFailedRunCanBeRetriedTwiceAndAPausedRunIsSkipped() {
        var payee = payments.addPayee(account.getCustomerId(), "Landlord", "11-22-33", "12345678");
        ScheduledPayment sp = payments.schedule(account.getId(), payee.getId(), "RENT", new BigDecimal("1200.00"),
                Frequency.MONTHLY, today, now.minus(5, ChronoUnit.DAYS));
        assertThat(payments.runDue(sp.getId(), now).getFailureReason()).isEqualTo(PaymentFailureReason.INSUFFICIENT_FUNDS);
        assertThat(payments.scheduledPayment(sp.getId()).getNextRun()).isEqualTo(today.plusMonths(1));
        assertThat(payments.retryFailedRun(sp.getId(), now).getRetryCount()).isEqualTo(1);
        accounts.receiveTransfer(account.getId(), new BigDecimal("500.00"), "Gift", now);
        var second = payments.retryFailedRun(sp.getId(), now);
        assertThat(second.getResult()).isEqualTo(PaymentAttemptResult.SUCCEEDED);
        assertThat(second.getRunDate()).isEqualTo(today);
        assertThatThrownBy(() -> payments.retryFailedRun(sp.getId(), now)).isInstanceOf(DomainException.class);
        assertThatThrownBy(() -> payments.runDue(sp.getId(), now)).as("not due yet").isInstanceOf(DomainException.class);

        ScheduledPayment once = payments.schedule(account.getId(), payee.getId(), "DEPOSIT", new BigDecimal("10.00"),
                Frequency.ONCE, today, now);
        payments.pause(once.getId());
        assertThat(payments.runDue(once.getId(), now).getResult()).isEqualTo(PaymentAttemptResult.SKIPPED);
        assertThat(payments.scheduledPayment(once.getId()).getStatus()).isEqualTo(ScheduledPaymentStatus.COMPLETED);
    }
}
