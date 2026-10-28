package com.claudepoc.banking.simulation;

import com.claudepoc.banking.api.BadRequestException;
import com.claudepoc.banking.domain.Enums;
import com.claudepoc.banking.simulation.ScenarioCatalog.Persona;
import com.claudepoc.banking.simulation.ScenarioCatalog.Scenario;
import com.claudepoc.banking.simulation.SimulationEnums.IssueType;
import com.claudepoc.banking.simulation.SimulationEnums.RootCause;
import com.claudepoc.banking.situations.SituationParams;
import com.claudepoc.banking.situations.SituationResult;
import com.claudepoc.banking.situations.Situations;
import com.claudepoc.banking.situations.Situations.Subject;
import com.claudepoc.banking.situations.Timeline;
import java.time.Clock;
import java.time.Instant;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.ThreadLocalRandom;
import org.springframework.context.annotation.Profile;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

/** Creates incidents: picks a scenario, sets it up through the domain, returns the answer key. */
@Service
@Profile("simulation")
public class IncidentService {

    public record IncidentRequest(String type, String rootCause, UUID customerId, Map<String, Object> params) {
    }

    public record Incident(UUID incidentId, String issueType, UUID customerId, String customerName,
                           Map<String, Object> personaHints, List<String> customerKnowledge, String complaint,
                           Map<String, UUID> affected, String expectedRootCause, String expectedOutcome,
                           Instant createdAt) {
    }

    private static final int MAX_TURNS = 6;

    private final Situations situations;
    private final Clock clock;

    public IncidentService(Situations situations, Clock clock) {
        this.situations = situations;
        this.clock = clock;
    }

    /** All or nothing: if the rules do not produce the intended outcome, nothing is saved. */
    @Transactional
    public Incident create(IncidentRequest request) {
        IncidentRequest r = request == null ? new IncidentRequest(null, null, null, null) : request;
        Scenario scenario = choose(r.type(), r.rootCause());
        Timeline t = new Timeline(Instant.now(clock));
        Subject subject = r.customerId() == null
                ? situations.newTestCustomer(t)
                : situations.existingCustomer(r.customerId(), t);
        SituationResult result = scenario.setup().run(situations, subject, new SituationParams(r.params()), t);
        Persona persona = scenario.persona().apply(result);

        Map<String, Object> hints = new LinkedHashMap<>();
        hints.put("vagueness", persona.vagueness());
        hints.put("patience", persona.patience());
        hints.put("tone", persona.tone());
        hints.put("phrasing", persona.phrasing());
        hints.put("knows", persona.knows());
        hints.put("max_turns", MAX_TURNS);

        Map<String, UUID> affected = new LinkedHashMap<>();
        affected.put("account_id", result.accountId());
        if (result.cardId() != null) {
            affected.put("card_id", result.cardId());
        }
        if (result.scheduledPaymentId() != null) {
            affected.put("scheduled_payment_id", result.scheduledPaymentId());
        }
        if (result.mandateId() != null) {
            affected.put("mandate_id", result.mandateId());
        }
        return new Incident(UUID.randomUUID(), scenario.type().wire(), subject.customer().getId(),
                subject.customer().getName(), hints, persona.knows(), persona.complaint(), affected,
                scenario.rootCause().wire(), scenario.outcome().wire(), t.now());
    }

    static Scenario choose(String typeValue, String rootCauseValue) {
        IssueType type = parse(IssueType.class, "type", typeValue);
        RootCause rootCause = parse(RootCause.class, "root_cause", rootCauseValue);
        if (type == IssueType.OTHER) {
            throw new BadRequestException("type 'other' cannot be simulated");
        }
        List<Scenario> candidates = ScenarioCatalog.SCENARIOS.stream()
                .filter(s -> type == null || s.type() == type)
                .filter(s -> rootCause == null || s.rootCause() == rootCause)
                .toList();
        if (candidates.isEmpty()) {
            throw new BadRequestException("No simulation scenario for type " + (type == null ? "(any)" : type.wire())
                    + " and root_cause " + rootCause.wire());
        }
        if (type == null && rootCause == null) {
            // Random type first, then a random root cause of that type, so every type is equally likely.
            List<IssueType> types = List.of(IssueType.CARD_DECLINE, IssueType.MISSED_PAYMENT, IssueType.FAILED_DIRECT_DEBIT);
            candidates = ScenarioCatalog.ofType(types.get(ThreadLocalRandom.current().nextInt(types.size())));
        }
        return candidates.get(ThreadLocalRandom.current().nextInt(candidates.size()));
    }

    private static <E extends Enum<E> & Enums.Wire> E parse(Class<E> type, String name, String value) {
        if (value == null) {
            return null;
        }
        E parsed = Enums.fromWire(type, value);
        if (parsed == null || !parsed.wire().equals(value)) {
            throw new BadRequestException("Invalid " + name + ": " + value);
        }
        return parsed;
    }
}
