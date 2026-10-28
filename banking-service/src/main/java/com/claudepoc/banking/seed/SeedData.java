package com.claudepoc.banking.seed;

import com.claudepoc.banking.domain.Card;
import com.claudepoc.banking.domain.Repositories.CustomerRepository;
import com.claudepoc.banking.situations.SituationParams;
import com.claudepoc.banking.situations.Situations;
import com.claudepoc.banking.situations.Situations.Subject;
import com.claudepoc.banking.situations.Timeline;
import java.math.BigDecimal;
import java.time.Clock;
import java.time.Instant;
import java.util.List;
import java.util.UUID;
import java.util.function.BiConsumer;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.ApplicationArguments;
import org.springframework.boot.ApplicationRunner;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.stereotype.Component;
import org.springframework.transaction.support.TransactionTemplate;

/**
 * Seeds the customers for the "log in as" picker on an empty database. Everything is created
 * through the domain operations (see {@link Situations}), so declines and failures come from
 * the rules. The ids are fixed so that they stay the same across fresh databases.
 *
 * <p>Disable with {@code BANKING_SEED_ENABLED=false}.
 */
@Component
@ConditionalOnProperty(name = "banking.seed.enabled", havingValue = "true", matchIfMissing = true)
public class SeedData implements ApplicationRunner {

    private static final Logger log = LoggerFactory.getLogger(SeedData.class);

    /** A seeded customer: fixed id, profile, and what has happened on their account. */
    public record SeedCustomer(UUID id, String name, String email, boolean vulnerable,
                               BiConsumer<Subject, Timeline> story) {
    }

    private final Situations situations;
    private final CustomerRepository customers;
    private final TransactionTemplate tx;
    private final Clock clock;

    public SeedData(Situations situations, CustomerRepository customers, TransactionTemplate tx, Clock clock) {
        this.situations = situations;
        this.customers = customers;
        this.tx = tx;
        this.clock = clock;
    }

    public static UUID seedId(int n) {
        return UUID.fromString(String.format("00000000-0000-4000-8000-%012d", n));
    }

    public List<SeedCustomer> customers() {
        SituationParams none = SituationParams.none();
        return List.of(
                new SeedCustomer(seedId(1), "Alice Morgan", "alice.morgan@example.com", false, this::healthy),
                new SeedCustomer(seedId(2), "Ben Carter", "ben.carter@example.com", false,
                        (s, t) -> situations.cardFrozenByCustomer(s, none, t)),
                new SeedCustomer(seedId(3), "Chloe Davies", "chloe.davies@example.com", false,
                        (s, t) -> situations.missedPaymentInsufficientFunds(s, none, t)),
                new SeedCustomer(seedId(4), "Daniel Evans", "daniel.evans@example.com", false,
                        (s, t) -> situations.directDebitMandateCancelled(s, none, t)),
                new SeedCustomer(seedId(5), "Emily Foster", "emily.foster@example.com", false,
                        (s, t) -> situations.fraudRuleTriggered(s, none, t)),
                new SeedCustomer(seedId(6), "Farah Ghani", "farah.ghani@example.com", false,
                        (s, t) -> situations.cardExpired(s, none, t)),
                new SeedCustomer(seedId(7), "George Harris", "george.harris@example.com", true,
                        (s, t) -> situations.cardInsufficientFunds(s, none, t)),
                new SeedCustomer(seedId(8), "Hannah Ibrahim", "hannah.ibrahim@example.com", false,
                        (s, t) -> situations.missedPaymentPayeeClosed(s, none, t)),
                new SeedCustomer(seedId(9), "Isaac Jones", "isaac.jones@example.com", false,
                        (s, t) -> situations.merchantCategoryBlocked(s, none, t)),
                new SeedCustomer(seedId(10), "Julia King", "julia.king@example.com", false,
                        (s, t) -> situations.directDebitAccountClosedByOriginator(s, none, t)),
                new SeedCustomer(seedId(11), "Kofi Mensah", "kofi.mensah@example.com", false,
                        (s, t) -> situations.contactlessLimitExceeded(s, none, t)),
                new SeedCustomer(seedId(12), "Laura Nowak", "laura.nowak@example.com", false,
                        (s, t) -> situations.directDebitInsufficientFunds(s, none, t)),
                new SeedCustomer(seedId(13), "Mark Owen", "mark.owen@example.com", false,
                        (s, t) -> situations.missedPaymentDailyLimit(s, none, t)));
    }

    @Override
    public void run(ApplicationArguments args) {
        tx.executeWithoutResult(status -> {
            if (customers.count() > 0) {
                log.info("Seed data skipped: the database already has customers");
                return;
            }
            Timeline t = new Timeline(Instant.now(clock));
            for (SeedCustomer c : customers()) {
                Subject subject = situations.newCustomer(c.id(), c.name(), c.email(), c.vulnerable(), t);
                c.story().accept(subject, t);
            }
            log.info("Seeded {} customers", customers().size());
        });
    }

    /** No problems: a standing order and a direct debit that both paid this month. */
    private void healthy(Subject s, Timeline t) {
        situations.standingOrderWithHistory(s, "Oakwood Property", "RENT", new BigDecimal("950.00"), t);
        situations.mandateWithHistory(s, "Northern Power", new BigDecimal("85.00"), t);
        Card card = situations.activeCard(s, t);
        situations.spend(card, 1, "Waitrose", "5411", com.claudepoc.banking.domain.Enums.Channel.CHIP_AND_PIN, "41.75", t);
    }
}
