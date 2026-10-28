package com.claudepoc.banking.situations;

import com.claudepoc.banking.domain.Money;
import java.math.BigDecimal;
import java.util.Map;

/** Optional scenario settings (`params` of a simulation request), read with defaults and validated. */
public final class SituationParams {

    private final Map<String, Object> values;

    public SituationParams(Map<String, Object> values) {
        this.values = values == null ? Map.of() : Map.copyOf(values);
    }

    public static SituationParams none() {
        return new SituationParams(Map.of());
    }

    public boolean has(String name) {
        return values.containsKey(name) && values.get(name) != null;
    }

    public String string(String name, String fallback) {
        Object v = values.get(name);
        if (v == null) {
            return fallback;
        }
        if (!(v instanceof String s) || s.isBlank() || s.length() > 120) {
            throw new SituationException("params." + name + " must be a non-empty string");
        }
        return s.trim();
    }

    public String mcc(String name, String fallback) {
        String value = string(name, fallback);
        if (!value.matches("^\\d{4}$")) {
            throw new SituationException("params." + name + " must be a four-digit merchant category code");
        }
        return value;
    }

    public BigDecimal money(String name, BigDecimal fallback) {
        Object v = values.get(name);
        if (v == null) {
            return fallback == null ? null : Money.normalise(fallback);
        }
        try {
            BigDecimal amount = new BigDecimal(v.toString());
            if (amount.signum() <= 0 || amount.scale() > 2) {
                throw new NumberFormatException();
            }
            return Money.normalise(amount);
        } catch (NumberFormatException e) {
            throw new SituationException("params." + name + " must be a positive amount with at most two decimals");
        }
    }

    public int integer(String name, int fallback, int min, int max) {
        Object v = values.get(name);
        if (v == null) {
            return fallback;
        }
        if (v instanceof Number n && n.doubleValue() == n.intValue() && n.intValue() >= min && n.intValue() <= max) {
            return n.intValue();
        }
        throw new SituationException("params." + name + " must be an integer from " + min + " to " + max);
    }
}
