package com.claudepoc.banking.domain;

import java.time.Instant;
import java.util.Collection;
import java.util.List;
import java.util.UUID;
import org.springframework.data.jpa.repository.JpaRepository;

/** Spring Data repositories for the banking tables. */
public final class Repositories {

    private Repositories() {
    }

    public interface CustomerRepository extends JpaRepository<Customer, UUID> {
        List<Customer> findAllByOrderByCreatedAtAscNameAsc();
    }

    public interface AccountRepository extends JpaRepository<Account, UUID> {
        List<Account> findByCustomerIdOrderByOpenedAtAsc(UUID customerId);
        boolean existsBySortCodeAndAccountNumber(String sortCode, String accountNumber);
    }

    public interface CardRepository extends JpaRepository<Card, UUID> {
        List<Card> findByAccountIdInOrderByIssuedAtDesc(Collection<UUID> accountIds);
    }

    public interface CardAuthorisationRepository extends JpaRepository<CardAuthorisation, UUID> {
        List<CardAuthorisation> findByCardIdInOrderByCreatedAtDescIdDesc(Collection<UUID> cardIds);
        long countByCardIdAndCreatedAtBetween(UUID cardId, Instant from, Instant to);
    }

    public interface FraudFlagRepository extends JpaRepository<FraudFlag, UUID> {
        List<FraudFlag> findByCustomerIdOrderByCreatedAtDescIdDesc(UUID customerId);
    }

    public interface TransactionRepository extends JpaRepository<Transaction, UUID> {
        List<Transaction> findByAccountIdOrderByBookedAtDescIdDesc(UUID accountId);
        List<Transaction> findByAccountIdAndBookedAtBetween(UUID accountId, Instant from, Instant to);
    }

    public interface PayeeRepository extends JpaRepository<Payee, UUID> {
        List<Payee> findByCustomerIdOrderByNameAsc(UUID customerId);
    }

    public interface ScheduledPaymentRepository extends JpaRepository<ScheduledPayment, UUID> {
        List<ScheduledPayment> findByAccountIdInOrderByCreatedAtDescIdDesc(Collection<UUID> accountIds);
    }

    public interface PaymentAttemptRepository extends JpaRepository<PaymentAttempt, UUID> {
        List<PaymentAttempt> findByScheduledPaymentIdInOrderByAttemptedAtDescRetryCountDescIdDesc(Collection<UUID> ids);
        List<PaymentAttempt> findByScheduledPaymentIdAndRunDateOrderByRetryCountDesc(UUID id, java.time.LocalDate runDate);
    }

    public interface MandateRepository extends JpaRepository<Mandate, UUID> {
        List<Mandate> findByAccountIdInOrderByCreatedAtDescIdDesc(Collection<UUID> accountIds);
    }

    public interface DirectDebitCollectionRepository extends JpaRepository<DirectDebitCollection, UUID> {
        List<DirectDebitCollection> findByMandateIdInOrderByDueDateDescPresentedAtDescIdDesc(Collection<UUID> mandateIds);
    }
}
