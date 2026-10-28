package com.claudepoc.banking.domain;

import java.math.BigDecimal;
import java.math.RoundingMode;

/** Money helpers: amounts are BigDecimal with two decimal places. */
public final class Money {

    public static final BigDecimal ZERO = new BigDecimal("0.00");

    private Money() {
    }

    public static BigDecimal of(String value) {
        return normalise(new BigDecimal(value));
    }

    public static BigDecimal normalise(BigDecimal value) {
        return value.setScale(2, RoundingMode.HALF_EVEN);
    }

    /** Contract format: a decimal string with two places, e.g. "12.50" or "-3.00". */
    public static String format(BigDecimal value) {
        return normalise(value).toPlainString();
    }
}
