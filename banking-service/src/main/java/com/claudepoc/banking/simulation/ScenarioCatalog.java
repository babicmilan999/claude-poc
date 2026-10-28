package com.claudepoc.banking.simulation;

import com.claudepoc.banking.simulation.SimulationEnums.ExpectedOutcome;
import com.claudepoc.banking.simulation.SimulationEnums.IssueType;
import com.claudepoc.banking.simulation.SimulationEnums.RootCause;
import com.claudepoc.banking.situations.SituationParams;
import com.claudepoc.banking.situations.SituationResult;
import com.claudepoc.banking.situations.Situations;
import com.claudepoc.banking.situations.Situations.Subject;
import com.claudepoc.banking.situations.Timeline;
import java.util.List;
import java.util.Optional;
import java.util.function.Function;

/**
 * The simulation scenarios: one per row of architecture section 5, each with its answer key
 * (expected root cause and outcome), the situation that sets it up through the domain, and
 * what the simulated customer would know. Customer knowledge never states the root cause
 * unless a real customer would know it.
 */
public final class ScenarioCatalog {

    @FunctionalInterface
    public interface Setup {
        SituationResult run(Situations situations, Subject subject, SituationParams params, Timeline t);
    }

    public record Persona(String vagueness, String patience, String tone, List<String> phrasing, List<String> knows,
                          String complaint) {
    }

    public record Scenario(IssueType type, RootCause rootCause, ExpectedOutcome outcome, Setup setup,
                           Function<SituationResult, Persona> persona) {
    }

    public static final List<Scenario> SCENARIOS = List.of(
            // ---- card declines
            new Scenario(IssueType.CARD_DECLINE, RootCause.CARD_FROZEN_BY_CUSTOMER, ExpectedOutcome.SELF_SERVE,
                    Situations::cardFrozenByCustomer,
                    r -> new Persona("medium", "medium", "frustrated",
                            List.of("my card keeps getting declined", "it just says declined at the till"),
                            List.of("the card ending " + r.fact("card_last4") + " was declined " + r.fact("declined_attempts")
                                            + " time(s) today at " + r.fact("merchant_name"),
                                    "the payment was for £" + r.fact("amount"),
                                    "there is enough money in the account",
                                    "they used the banking app last week but do not remember changing anything"),
                            "My card was declined at " + r.fact("merchant_name") + " today even though I have money "
                                    + "in my account. What is going on?")),
            new Scenario(IssueType.CARD_DECLINE, RootCause.INSUFFICIENT_FUNDS, ExpectedOutcome.SELF_SERVE,
                    Situations::cardInsufficientFunds,
                    r -> new Persona("medium", "medium", "embarrassed",
                            List.of("my card got declined", "why won't my card work?"),
                            List.of("the card was declined at " + r.fact("merchant_name") + " today for £" + r.fact("amount"),
                                    "they thought they had enough money but do not know the exact balance",
                                    "payday is next week"),
                            "My card was declined at " + r.fact("merchant_name") + " today. Can you tell me why?")),
            new Scenario(IssueType.CARD_DECLINE, RootCause.CARD_EXPIRED, ExpectedOutcome.SELF_SERVE,
                    Situations::cardExpired,
                    r -> new Persona("high", "medium", "confused",
                            List.of("my card stopped working", "the card machine said declined"),
                            List.of("the card ending " + r.fact("card_last4") + " was declined at "
                                            + r.fact("merchant_name") + " today",
                                    "the same card worked fine last month",
                                    "a letter from the bank arrived a few weeks ago but they have not opened it"),
                            "My card suddenly stopped working at " + r.fact("merchant_name")
                                    + ". It worked last month. Please help.")),
            new Scenario(IssueType.CARD_DECLINE, RootCause.CONTACTLESS_LIMIT_EXCEEDED, ExpectedOutcome.SELF_SERVE,
                    Situations::contactlessLimitExceeded,
                    r -> new Persona("medium", "high", "polite",
                            List.of("tapping my card didn't work", "my card was declined when I tapped it"),
                            List.of("they tapped the card at " + r.fact("merchant_name") + " for £" + r.fact("amount")
                                            + " and it was declined",
                                    "other card payments this week worked",
                                    "they changed some card settings in the app a while ago"),
                            "I tried to pay by tapping my card at " + r.fact("merchant_name")
                                    + " and it was declined. Why?")),
            new Scenario(IssueType.CARD_DECLINE, RootCause.MERCHANT_CATEGORY_BLOCKED, ExpectedOutcome.SELF_SERVE,
                    Situations::merchantCategoryBlocked,
                    r -> new Persona("medium", "medium", "puzzled",
                            List.of("a website won't take my card", "my online payment was refused"),
                            List.of("they tried to pay " + r.fact("merchant_name") + " online for £" + r.fact("amount")
                                            + " and it was declined",
                                    "other online shopping works",
                                    "they set up some spending controls in the app some time ago"),
                            "My card was refused by " + r.fact("merchant_name")
                                    + " online, but other websites work. What is wrong?")),
            new Scenario(IssueType.CARD_DECLINE, RootCause.FRAUD_RULE_TRIGGERED, ExpectedOutcome.NEEDS_HUMAN,
                    Situations::fraudRuleTriggered,
                    r -> new Persona("low", "low", "anxious",
                            List.of("my card doesn't work anywhere", "my card has been stopped"),
                            List.of("they tried to pay £" + r.fact("amount") + " to " + r.fact("merchant_name")
                                            + " online today and it was declined",
                                    "later the card was declined at the supermarket too",
                                    "they did not get any message from the bank"),
                            "My card was declined online and now it does not work anywhere, not even at the "
                                    + "supermarket. I need it working today.")),
            // ---- missed payments
            new Scenario(IssueType.MISSED_PAYMENT, RootCause.INSUFFICIENT_FUNDS, ExpectedOutcome.SELF_SERVE,
                    Situations::missedPaymentInsufficientFunds,
                    r -> new Persona("medium", "medium", "worried",
                            List.of("my rent didn't go out", "my standing order didn't pay"),
                            List.of(r.fact("payee_name") + " says this month's rent of £" + r.fact("amount")
                                            + " has not arrived",
                                    "it is a standing order that has worked for months",
                                    "they spent more than usual this month (flights and a new laptop)"),
                            "My landlord says my rent did not arrive this month. It is a standing order and it "
                                    + "always works. Please check.")),
            new Scenario(IssueType.MISSED_PAYMENT, RootCause.DAILY_PAYMENT_LIMIT_EXCEEDED, ExpectedOutcome.SELF_SERVE,
                    Situations::missedPaymentDailyLimit,
                    r -> new Persona("medium", "medium", "frustrated",
                            List.of("my rent payment didn't happen", "the standing order failed"),
                            List.of("the rent of £" + r.fact("amount") + " to " + r.fact("payee_name")
                                            + " did not go out today",
                                    "the standing order normally works",
                                    "they moved £" + r.fact("transfer_amount") + " to savings early this morning",
                                    "they changed some security settings in the app a couple of weeks ago"),
                            "My rent standing order did not go out today. Why did it fail?")),
            new Scenario(IssueType.MISSED_PAYMENT, RootCause.PAYEE_ACCOUNT_CLOSED, ExpectedOutcome.NEEDS_HUMAN,
                    Situations::missedPaymentPayeeClosed,
                    r -> new Persona("medium", "low", "frustrated",
                            List.of("my rent payment failed", "the money never reached my landlord"),
                            List.of(r.fact("payee_name") + " says the rent of £" + r.fact("amount") + " did not arrive",
                                    "the standing order has worked for months",
                                    "the money is still in their account"),
                            "My rent to " + r.fact("payee_name") + " did not arrive but the money is still in my "
                                    + "account. What happened?")),
            // ---- failed direct debits
            new Scenario(IssueType.FAILED_DIRECT_DEBIT, RootCause.MANDATE_CANCELLED_BY_CUSTOMER, ExpectedOutcome.SELF_SERVE,
                    Situations::directDebitMandateCancelled,
                    r -> new Persona("high", "low", "anxious",
                            List.of("my rent didn't go out", "the payment to my landlord failed"),
                            List.of(r.fact("originator") + " says this month's payment of £" + r.fact("amount")
                                            + " was not made",
                                    "the payment is normally taken automatically",
                                    "they cancelled some old direct debits a few weeks ago"),
                            "My letting agent says my rent didn't go out this month. It always goes out "
                                    + "automatically. Please look into it.")),
            new Scenario(IssueType.FAILED_DIRECT_DEBIT, RootCause.INSUFFICIENT_FUNDS, ExpectedOutcome.SELF_SERVE,
                    Situations::directDebitInsufficientFunds,
                    r -> new Persona("medium", "medium", "worried",
                            List.of("my direct debit bounced", "my bill wasn't paid"),
                            List.of(r.fact("originator") + " says the direct debit of £" + r.fact("amount") + " failed",
                                    "they spent quite a lot this week (flights and a new laptop)"),
                            r.fact("originator") + " says my direct debit failed. Why was it not paid?")),
            new Scenario(IssueType.FAILED_DIRECT_DEBIT, RootCause.ACCOUNT_CLOSED_BY_ORIGINATOR, ExpectedOutcome.NEEDS_HUMAN,
                    Situations::directDebitAccountClosedByOriginator,
                    r -> new Persona("medium", "medium", "confused",
                            List.of("my direct debit failed", "a payment I didn't cancel stopped"),
                            List.of(r.fact("originator") + " emailed that the direct debit of £" + r.fact("amount")
                                            + " failed",
                                    "they did not cancel anything",
                                    "they recently moved house and updated their details with " + r.fact("originator")),
                            "I got an email from " + r.fact("originator") + " saying my direct debit failed, but I "
                                    + "never cancelled it. Can you find out what happened?")));

    private ScenarioCatalog() {
    }

    public static List<Scenario> ofType(IssueType type) {
        return SCENARIOS.stream().filter(s -> s.type() == type).toList();
    }

    public static Optional<Scenario> find(IssueType type, RootCause rootCause) {
        return SCENARIOS.stream().filter(s -> s.type() == type && s.rootCause() == rootCause).findFirst();
    }
}
