package com.claudepoc.banking.rules;

import static org.assertj.core.api.Assertions.assertThat;

import com.claudepoc.banking.domain.Account;
import com.claudepoc.banking.domain.Card;
import com.claudepoc.banking.domain.Enums.AccountStatus;
import com.claudepoc.banking.domain.Enums.CancelledBy;
import com.claudepoc.banking.domain.Enums.CardBrand;
import com.claudepoc.banking.domain.Enums.CardStatus;
import com.claudepoc.banking.domain.Enums.CardStatusReason;
import com.claudepoc.banking.domain.Enums.Channel;
import com.claudepoc.banking.domain.Enums.CollectionFailureCode;
import com.claudepoc.banking.domain.Enums.DeclineReason;
import com.claudepoc.banking.domain.Enums.FraudAction;
import com.claudepoc.banking.domain.Enums.PayeeAccountStatus;
import com.claudepoc.banking.domain.Enums.PaymentFailureReason;
import com.claudepoc.banking.domain.Mandate;
import com.claudepoc.banking.domain.Payee;
import java.math.BigDecimal;
import java.time.Instant;
import java.time.LocalDate;
import java.util.List;
import java.util.UUID;
import org.junit.jupiter.api.Nested;
import org.junit.jupiter.api.Test;

/** The decision rules on their own: each reason code, and the order in which rules apply. */
class RulesTest {

    private static final LocalDate TODAY = LocalDate.of(2026, 10, 28);
    private static final Instant NOW = Instant.parse("2026-10-28T12:00:00Z");

    private static Account account(String balance) {
        Account a = new Account(UUID.randomUUID(), UUID.randomUUID(), "Current account", "20-45-77", "12345678", "GBP",
                BigDecimal.ZERO, new BigDecimal("1000.00"), NOW);
        a.applyAmount(new BigDecimal(balance));
        return a;
    }

    private static Card card() {
        return new Card(UUID.randomUUID(), UUID.randomUUID(), "1234", CardBrand.VISA, TODAY.plusYears(2),
                new BigDecimal("100.00"), NOW);
    }

    private static BigDecimal gbp(String v) {
        return new BigDecimal(v);
    }

    @Nested
    class Authorisation {

        @Test
        void approvesWhenNothingFails() {
            assertThat(AuthorisationRules.cardChecks(account("50.00"), card(), gbp("20.00"), "5411", Channel.CHIP_AND_PIN, TODAY)).isEmpty();
            assertThat(AuthorisationRules.fundsCheck(account("50.00"), gbp("50.00"))).isEmpty();
        }

        @Test
        void eachCardCheckGivesItsReason() {
            Account ok = account("500.00");
            Card frozen = card();
            frozen.changeStatus(CardStatus.FROZEN, CardStatusReason.FROZEN_BY_CUSTOMER);
            assertThat(AuthorisationRules.cardChecks(ok, frozen, gbp("1.00"), "5411", Channel.CHIP_AND_PIN, TODAY)).contains(DeclineReason.CARD_FROZEN);

            Card blocked = card();
            blocked.changeStatus(CardStatus.BLOCKED, CardStatusReason.LOST_OR_STOLEN);
            assertThat(AuthorisationRules.cardChecks(ok, blocked, gbp("1.00"), "5411", Channel.CHIP_AND_PIN, TODAY)).contains(DeclineReason.CARD_BLOCKED);

            Card expired = new Card(UUID.randomUUID(), UUID.randomUUID(), "1234", CardBrand.VISA, TODAY.minusDays(1), gbp("100.00"), NOW);
            assertThat(AuthorisationRules.cardChecks(ok, expired, gbp("1.00"), "5411", Channel.CHIP_AND_PIN, TODAY)).contains(DeclineReason.CARD_EXPIRED);
            Card lastDay = new Card(UUID.randomUUID(), UUID.randomUUID(), "1234", CardBrand.VISA, TODAY, gbp("100.00"), NOW);
            assertThat(AuthorisationRules.cardChecks(ok, lastDay, gbp("1.00"), "5411", Channel.CHIP_AND_PIN, TODAY))
                    .as("the expiry date is the last valid day").isEmpty();

            Card noOnline = card();
            noOnline.setOnlinePaymentsEnabled(false);
            assertThat(AuthorisationRules.cardChecks(ok, noOnline, gbp("1.00"), "5411", Channel.ONLINE, TODAY)).contains(DeclineReason.ONLINE_PAYMENTS_DISABLED);
            assertThat(AuthorisationRules.cardChecks(ok, noOnline, gbp("1.00"), "5411", Channel.CHIP_AND_PIN, TODAY)).isEmpty();

            Card mcc = card();
            mcc.setBlockedMerchantCategories(List.of("7995"));
            assertThat(AuthorisationRules.cardChecks(ok, mcc, gbp("1.00"), "7995", Channel.CHIP_AND_PIN, TODAY)).contains(DeclineReason.MERCHANT_CATEGORY_BLOCKED);

            assertThat(AuthorisationRules.cardChecks(ok, card(), gbp("100.01"), "5411", Channel.CONTACTLESS, TODAY)).contains(DeclineReason.CONTACTLESS_LIMIT_EXCEEDED);
            assertThat(AuthorisationRules.cardChecks(ok, card(), gbp("100.00"), "5411", Channel.CONTACTLESS, TODAY)).isEmpty();
            assertThat(AuthorisationRules.cardChecks(ok, card(), gbp("100.01"), "5411", Channel.CHIP_AND_PIN, TODAY)).isEmpty();

            Account frozenAccount = account("500.00");
            frozenAccount.setStatus(AccountStatus.FROZEN);
            assertThat(AuthorisationRules.cardChecks(frozenAccount, card(), gbp("1.00"), "5411", Channel.CHIP_AND_PIN, TODAY)).contains(DeclineReason.ACCOUNT_NOT_ACTIVE);
        }

        @Test
        void fundsIncludeTheOverdraft() {
            Account a = account("10.00");
            assertThat(AuthorisationRules.fundsCheck(a, gbp("10.01"))).contains(DeclineReason.INSUFFICIENT_FUNDS);
            a.setOverdraftLimit(gbp("100.00"));
            assertThat(AuthorisationRules.fundsCheck(a, gbp("110.00"))).isEmpty();
        }

        @Test
        void aFrozenCardIsReportedBeforeOtherProblems() {
            Card c = card();
            c.changeStatus(CardStatus.FROZEN, CardStatusReason.FROZEN_BY_CUSTOMER);
            c.setBlockedMerchantCategories(List.of("5411"));
            assertThat(AuthorisationRules.cardChecks(account("0.00"), c, gbp("500.00"), "5411", Channel.CONTACTLESS, TODAY))
                    .contains(DeclineReason.CARD_FROZEN);
        }
    }

    @Nested
    class Fraud {

        @Test
        void highValueOnlineHighRiskIsFr12AndBlocksTheCard() {
            assertThat(FraudRules.evaluate(gbp("500.00"), "6051", Channel.ONLINE, 0)).hasValueSatisfying(h -> {
                assertThat(h.ruleId()).isEqualTo("FR-12");
                assertThat(h.action()).isEqualTo(FraudAction.CARD_BLOCKED);
            });
            assertThat(FraudRules.evaluate(gbp("499.99"), "6051", Channel.ONLINE, 0)).isEmpty();
            assertThat(FraudRules.evaluate(gbp("900.00"), "6051", Channel.CHIP_AND_PIN, 0)).isEmpty();
            assertThat(FraudRules.evaluate(gbp("900.00"), "4511", Channel.ONLINE, 0)).isEmpty();
        }

        @Test
        void manyPaymentsInAShortTimeIsFr07() {
            assertThat(FraudRules.evaluate(gbp("5.00"), "5411", Channel.CONTACTLESS, 5)).hasValueSatisfying(h -> assertThat(h.ruleId()).isEqualTo("FR-07"));
            assertThat(FraudRules.evaluate(gbp("5.00"), "5411", Channel.CONTACTLESS, 4)).isEmpty();
        }
    }

    @Nested
    class Payments {

        private Payee payee(PayeeAccountStatus status) {
            return new Payee(UUID.randomUUID(), UUID.randomUUID(), "Oakwood Lettings", "11-22-33", "87654321", status);
        }

        @Test
        void eachReasonInOrder() {
            Account a = account("900.00");
            assertThat(PaymentRules.check(a, payee(PayeeAccountStatus.OPEN), gbp("850.00"), BigDecimal.ZERO)).isEmpty();
            assertThat(PaymentRules.check(a, payee(PayeeAccountStatus.OPEN), gbp("950.00"), BigDecimal.ZERO)).contains(PaymentFailureReason.INSUFFICIENT_FUNDS);
            assertThat(PaymentRules.check(a, payee(PayeeAccountStatus.OPEN), gbp("850.00"), gbp("150.01"))).contains(PaymentFailureReason.DAILY_PAYMENT_LIMIT_EXCEEDED);
            assertThat(PaymentRules.check(a, payee(PayeeAccountStatus.OPEN), gbp("850.00"), gbp("150.00"))).isEmpty();
            assertThat(PaymentRules.check(a, payee(PayeeAccountStatus.CLOSED), gbp("9999.00"), gbp("9999.00"))).contains(PaymentFailureReason.PAYEE_ACCOUNT_CLOSED);
            assertThat(PaymentRules.check(a, payee(PayeeAccountStatus.UNKNOWN), gbp("1.00"), BigDecimal.ZERO)).contains(PaymentFailureReason.INVALID_PAYEE_DETAILS);
            a.setStatus(AccountStatus.CLOSED);
            assertThat(PaymentRules.check(a, payee(PayeeAccountStatus.CLOSED), gbp("1.00"), BigDecimal.ZERO)).contains(PaymentFailureReason.ACCOUNT_NOT_ACTIVE);
        }
    }

    @Nested
    class Collections {

        private Mandate mandate() {
            return new Mandate(UUID.randomUUID(), UUID.randomUUID(), "Acme Lettings", "ACME-1", NOW);
        }

        @Test
        void eachFailureCode() {
            Account a = account("100.00");
            assertThat(CollectionRules.check(mandate(), a, gbp("100.00"))).isEmpty();
            assertThat(CollectionRules.check(mandate(), a, gbp("100.01"))).contains(CollectionFailureCode.INSUFFICIENT_FUNDS);

            Mandate byCustomer = mandate();
            byCustomer.cancel(CancelledBy.CUSTOMER, NOW);
            assertThat(CollectionRules.check(byCustomer, a, gbp("1.00"))).contains(CollectionFailureCode.MANDATE_CANCELLED);
            Mandate byBank = mandate();
            byBank.cancel(CancelledBy.BANK, NOW);
            assertThat(CollectionRules.check(byBank, a, gbp("1.00"))).contains(CollectionFailureCode.MANDATE_CANCELLED);
            Mandate byOriginator = mandate();
            byOriginator.cancel(CancelledBy.ORIGINATOR, NOW);
            assertThat(CollectionRules.check(byOriginator, a, gbp("1.00"))).contains(CollectionFailureCode.ACCOUNT_CLOSED_BY_ORIGINATOR);
            Mandate dormant = mandate();
            dormant.markDormant();
            assertThat(CollectionRules.check(dormant, a, gbp("1.00"))).contains(CollectionFailureCode.NO_MANDATE);

            a.setStatus(AccountStatus.CLOSED);
            assertThat(CollectionRules.check(mandate(), a, gbp("1.00"))).contains(CollectionFailureCode.PAYER_ACCOUNT_CLOSED);
        }
    }
}
