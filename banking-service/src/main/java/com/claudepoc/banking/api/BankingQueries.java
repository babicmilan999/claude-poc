package com.claudepoc.banking.api;

import com.claudepoc.banking.domain.Account;
import com.claudepoc.banking.domain.Card;
import com.claudepoc.banking.domain.CardAuthorisation;
import com.claudepoc.banking.domain.Customer;
import com.claudepoc.banking.domain.DirectDebitCollection;
import com.claudepoc.banking.domain.Enums.AuthorisationResult;
import com.claudepoc.banking.domain.Enums.CollectionResult;
import com.claudepoc.banking.domain.Enums.FraudFlagStatus;
import com.claudepoc.banking.domain.Enums.MandateStatus;
import com.claudepoc.banking.domain.Enums.PaymentAttemptResult;
import com.claudepoc.banking.domain.FraudFlag;
import com.claudepoc.banking.domain.Mandate;
import com.claudepoc.banking.domain.NotFoundException;
import com.claudepoc.banking.domain.Payee;
import com.claudepoc.banking.domain.PaymentAttempt;
import com.claudepoc.banking.domain.Repositories.AccountRepository;
import com.claudepoc.banking.domain.Repositories.CardAuthorisationRepository;
import com.claudepoc.banking.domain.Repositories.CardRepository;
import com.claudepoc.banking.domain.Repositories.CustomerRepository;
import com.claudepoc.banking.domain.Repositories.DirectDebitCollectionRepository;
import com.claudepoc.banking.domain.Repositories.FraudFlagRepository;
import com.claudepoc.banking.domain.Repositories.MandateRepository;
import com.claudepoc.banking.domain.Repositories.PayeeRepository;
import com.claudepoc.banking.domain.Repositories.PaymentAttemptRepository;
import com.claudepoc.banking.domain.Repositories.ScheduledPaymentRepository;
import com.claudepoc.banking.domain.Repositories.TransactionRepository;
import com.claudepoc.banking.domain.ScheduledPayment;
import com.claudepoc.banking.domain.Transaction;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.Collection;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.function.Function;
import java.util.stream.Collectors;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

/**
 * Read-only queries behind the API. Every customer-owned record is looked up through the
 * customer, so an id owned by someone else is a {@link NotFoundException}.
 */
@Service
@Transactional(readOnly = true)
public class BankingQueries {

    public record ScheduledPaymentView(ScheduledPayment payment, Payee payee, PaymentAttempt lastAttempt) {
    }

    public record CollectionView(DirectDebitCollection collection, Mandate mandate) {
    }

    private final CustomerRepository customers;
    private final AccountRepository accounts;
    private final TransactionRepository transactions;
    private final CardRepository cards;
    private final CardAuthorisationRepository authorisations;
    private final FraudFlagRepository fraudFlags;
    private final PayeeRepository payees;
    private final ScheduledPaymentRepository scheduledPayments;
    private final PaymentAttemptRepository attempts;
    private final MandateRepository mandates;
    private final DirectDebitCollectionRepository collections;

    public BankingQueries(CustomerRepository customers, AccountRepository accounts, TransactionRepository transactions,
                          CardRepository cards, CardAuthorisationRepository authorisations,
                          FraudFlagRepository fraudFlags, PayeeRepository payees,
                          ScheduledPaymentRepository scheduledPayments, PaymentAttemptRepository attempts,
                          MandateRepository mandates, DirectDebitCollectionRepository collections) {
        this.customers = customers;
        this.accounts = accounts;
        this.transactions = transactions;
        this.cards = cards;
        this.authorisations = authorisations;
        this.fraudFlags = fraudFlags;
        this.payees = payees;
        this.scheduledPayments = scheduledPayments;
        this.attempts = attempts;
        this.mandates = mandates;
        this.collections = collections;
    }

    public List<Customer> customers() {
        return customers.findAllByOrderByCreatedAtAscNameAsc();
    }

    public Customer customer(UUID customerId) {
        return customers.findById(customerId)
                .orElseThrow(() -> new NotFoundException("Customer " + customerId + " not found"));
    }

    public List<Account> accounts(UUID customerId) {
        customer(customerId);
        return accounts.findByCustomerIdOrderByOpenedAtAsc(customerId);
    }

    public Account account(UUID customerId, UUID accountId) {
        return accounts.findById(accountId)
                .filter(a -> a.getCustomerId().equals(customerId))
                .orElseThrow(() -> new NotFoundException("Account " + accountId + " not found for customer " + customerId));
    }

    public List<Transaction> transactions(UUID customerId, UUID accountId, Instant since, Instant until, int limit) {
        account(customerId, accountId);
        return transactions.findByAccountIdOrderByBookedAtDescIdDesc(accountId).stream()
                .filter(t -> since == null || !t.getBookedAt().isBefore(since))
                .filter(t -> until == null || t.getBookedAt().isBefore(until))
                .limit(limit)
                .toList();
    }

    public List<Card> cards(UUID customerId) {
        return cards.findByAccountIdInOrderByIssuedAtDesc(accountIds(customerId));
    }

    public Card card(UUID customerId, UUID cardId) {
        return cards.findById(cardId)
                .filter(c -> accountIds(customerId).contains(c.getAccountId()))
                .orElseThrow(() -> new NotFoundException("Card " + cardId + " not found for customer " + customerId));
    }

    public List<CardAuthorisation> authorisations(UUID customerId, UUID cardId, AuthorisationResult result,
                                                  Instant since, int limit) {
        Collection<UUID> cardIds = cardId != null
                ? List.of(card(customerId, cardId).getId())
                : cards(customerId).stream().map(Card::getId).toList();
        return authorisations.findByCardIdInOrderByCreatedAtDescIdDesc(cardIds).stream()
                .filter(a -> result == null || a.getResult() == result)
                .filter(a -> since == null || !a.getCreatedAt().isBefore(since))
                .limit(limit)
                .toList();
    }

    public List<FraudFlag> fraudFlags(UUID customerId, UUID cardId, FraudFlagStatus status) {
        customer(customerId);
        if (cardId != null) {
            card(customerId, cardId);
        }
        return fraudFlags.findByCustomerIdOrderByCreatedAtDescIdDesc(customerId).stream()
                .filter(f -> cardId == null || cardId.equals(f.getCardId()))
                .filter(f -> status == null || f.getStatus() == status)
                .toList();
    }

    public List<ScheduledPaymentView> scheduledPayments(UUID customerId) {
        List<ScheduledPayment> payments = scheduledPayments.findByAccountIdInOrderByCreatedAtDescIdDesc(accountIds(customerId));
        Map<UUID, Payee> payeesById = payees.findByCustomerIdOrderByNameAsc(customerId).stream()
                .collect(Collectors.toMap(Payee::getId, Function.identity()));
        Map<UUID, PaymentAttempt> lastAttempts = attempts
                .findByScheduledPaymentIdInOrderByAttemptedAtDescRetryCountDescIdDesc(payments.stream().map(ScheduledPayment::getId).toList())
                .stream()
                .collect(Collectors.toMap(PaymentAttempt::getScheduledPaymentId, Function.identity(), (first, later) -> first));
        return payments.stream()
                .map(p -> new ScheduledPaymentView(p, payeesById.get(p.getPayeeId()), lastAttempts.get(p.getId())))
                .toList();
    }

    public List<PaymentAttempt> paymentAttempts(UUID customerId, UUID scheduledPaymentId, Set<PaymentAttemptResult> results,
                                                Instant since, int limit) {
        List<UUID> ids = scheduledPayments.findByAccountIdInOrderByCreatedAtDescIdDesc(accountIds(customerId)).stream()
                .map(ScheduledPayment::getId).toList();
        if (scheduledPaymentId != null) {
            if (!ids.contains(scheduledPaymentId)) {
                throw new NotFoundException("Scheduled payment " + scheduledPaymentId + " not found for customer " + customerId);
            }
            ids = List.of(scheduledPaymentId);
        }
        return attempts.findByScheduledPaymentIdInOrderByAttemptedAtDescRetryCountDescIdDesc(ids).stream()
                .filter(a -> results.isEmpty() || results.contains(a.getResult()))
                .filter(a -> since == null || !a.getAttemptedAt().isBefore(since))
                .limit(limit)
                .toList();
    }

    public List<Payee> payees(UUID customerId) {
        customer(customerId);
        return payees.findByCustomerIdOrderByNameAsc(customerId);
    }

    public Payee payee(UUID customerId, UUID payeeId) {
        return payees.findById(payeeId)
                .filter(p -> p.getCustomerId().equals(customerId))
                .orElseThrow(() -> new NotFoundException("Payee " + payeeId + " not found for customer " + customerId));
    }

    public List<Mandate> mandates(UUID customerId, MandateStatus status) {
        return mandates.findByAccountIdInOrderByCreatedAtDescIdDesc(accountIds(customerId)).stream()
                .filter(m -> status == null || m.getStatus() == status)
                .toList();
    }

    /** Collections, newest due date first. {@code since} compares with the due date (start of day, UTC). */
    public List<CollectionView> collections(UUID customerId, UUID mandateId, CollectionResult result, Instant since,
                                            int limit) {
        Map<UUID, Mandate> mandatesById = mandates(customerId, null).stream()
                .collect(Collectors.toMap(Mandate::getId, Function.identity()));
        if (mandateId != null && !mandatesById.containsKey(mandateId)) {
            throw new NotFoundException("Mandate " + mandateId + " not found for customer " + customerId);
        }
        Collection<UUID> ids = mandateId != null ? List.of(mandateId) : mandatesById.keySet();
        return collections.findByMandateIdInOrderByDueDateDescPresentedAtDescIdDesc(ids).stream()
                .filter(c -> result == null || c.getResult() == result)
                .filter(c -> since == null || !c.getDueDate().atStartOfDay(ZoneOffset.UTC).toInstant().isBefore(since))
                .limit(limit)
                .map(c -> new CollectionView(c, mandatesById.get(c.getMandateId())))
                .toList();
    }

    private List<UUID> accountIds(UUID customerId) {
        return accounts(customerId).stream().map(Account::getId).toList();
    }
}
