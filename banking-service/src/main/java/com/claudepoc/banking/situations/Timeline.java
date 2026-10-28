package com.claudepoc.banking.situations;

import java.time.Duration;
import java.time.Instant;
import java.time.LocalDate;
import java.time.ZoneOffset;
import java.time.temporal.ChronoUnit;

/** Times relative to "now" for staging a situation's history. Never in the future. */
public record Timeline(Instant now) {

    public Timeline {
        now = now.truncatedTo(ChronoUnit.MICROS);
    }

    public LocalDate today() {
        return LocalDate.ofInstant(now, ZoneOffset.UTC);
    }

    /** A time on the day {@code days} ago, at the given hour (UTC). */
    public Instant daysAgo(int days, int hour) {
        Instant t = today().minusDays(days).atTime(hour, 0).toInstant(ZoneOffset.UTC);
        return t.isAfter(now) ? now : t;
    }

    /** A time on a date at the given hour (UTC), capped at now. */
    public Instant on(LocalDate date, int hour) {
        Instant t = date.atTime(hour, 0).toInstant(ZoneOffset.UTC);
        return t.isAfter(now) ? now : t;
    }

    /** A time earlier today: {@code fraction} (0..1) of the way from midnight to now. */
    public Instant earlierToday(double fraction) {
        Instant midnight = today().atStartOfDay(ZoneOffset.UTC).toInstant();
        long micros = Duration.between(midnight, now).toNanos() / 1000;
        return midnight.plus((long) (micros * fraction), ChronoUnit.MICROS);
    }
}
