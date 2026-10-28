package com.claudepoc.banking.rules;

import com.claudepoc.banking.domain.Enums.Channel;
import com.claudepoc.banking.domain.Enums.FraudAction;
import java.math.BigDecimal;
import java.util.Optional;
import java.util.Set;

/** The bank's card fraud rules. A hit declines the payment, raises a fraud flag and may block the card. */
public final class FraudRules {

    /** Merchant categories treated as high risk: quasi-cash and crypto, money transfer, betting. */
    public static final Set<String> HIGH_RISK_MCCS = Set.of("6051", "4829", "7995");
    public static final BigDecimal HIGH_RISK_ONLINE_THRESHOLD = new BigDecimal("500.00");
    /** Authorisations on one card within the velocity window that trigger FR-07. */
    public static final int VELOCITY_MAX_IN_WINDOW = 5;
    public static final java.time.Duration VELOCITY_WINDOW = java.time.Duration.ofMinutes(10);

    private FraudRules() {
    }

    public record FraudHit(String ruleId, String description, BigDecimal riskScore, FraudAction action) {
    }

    /**
     * @param recentAuthorisations authorisations on the same card within {@link #VELOCITY_WINDOW} before this one
     */
    public static Optional<FraudHit> evaluate(BigDecimal amount, String mcc, Channel channel, long recentAuthorisations) {
        if (channel == Channel.ONLINE && HIGH_RISK_MCCS.contains(mcc)
                && amount.compareTo(HIGH_RISK_ONLINE_THRESHOLD) >= 0) {
            return Optional.of(new FraudHit("FR-12",
                    "High-value online payment to a high-risk merchant category (" + mcc + ")",
                    new BigDecimal("0.920"), FraudAction.CARD_BLOCKED));
        }
        if (recentAuthorisations >= VELOCITY_MAX_IN_WINDOW) {
            return Optional.of(new FraudHit("FR-07",
                    "Unusual number of card payments in a short time",
                    new BigDecimal("0.780"), FraudAction.CARD_BLOCKED));
        }
        return Optional.empty();
    }
}
