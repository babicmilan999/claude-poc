package com.claudepoc.banking.operations;

import com.claudepoc.banking.domain.Account;
import com.claudepoc.banking.domain.DirectDebitCollection;
import com.claudepoc.banking.domain.DomainException;
import com.claudepoc.banking.domain.Enums.CancelledBy;
import com.claudepoc.banking.domain.Enums.CollectionFailureCode;
import com.claudepoc.banking.domain.Enums.CollectionResult;
import com.claudepoc.banking.domain.Enums.MandateStatus;
import com.claudepoc.banking.domain.Enums.TransactionType;
import com.claudepoc.banking.domain.Mandate;
import com.claudepoc.banking.domain.NotFoundException;
import com.claudepoc.banking.domain.Repositories.DirectDebitCollectionRepository;
import com.claudepoc.banking.domain.Repositories.MandateRepository;
import com.claudepoc.banking.rules.CollectionRules;
import java.math.BigDecimal;
import java.time.Instant;
import java.time.LocalDate;
import java.util.Optional;
import java.util.UUID;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

/**
 * Direct debits: mandates, collections the originator presents, and processing them on the due
 * date through {@link CollectionRules}. Presenting against a cancelled mandate is allowed (as in
 * Bacs, the originator may not know yet); processing then fails it.
 */
@Service
@Transactional
public class DirectDebitOperations {

    private final AccountOperations accounts;
    private final MandateRepository mandates;
    private final DirectDebitCollectionRepository collections;

    public DirectDebitOperations(AccountOperations accounts, MandateRepository mandates,
                                 DirectDebitCollectionRepository collections) {
        this.accounts = accounts;
        this.mandates = mandates;
        this.collections = collections;
    }

    public Mandate setUpMandate(UUID accountId, String originator, String reference, Instant at) {
        Account account = accounts.account(accountId);
        if (!account.isActive()) {
            throw new DomainException("Cannot set up a mandate on a non-active account");
        }
        return mandates.save(new Mandate(UUID.randomUUID(), accountId, originator, reference, at));
    }

    /** The customer cancels the mandate in the app. */
    public void cancelByCustomer(UUID mandateId, Instant at) {
        cancel(mandateId, CancelledBy.CUSTOMER, at);
    }

    /** The bank cancels the mandate (for example after repeated returns). */
    public void cancelByBank(UUID mandateId, Instant at) {
        cancel(mandateId, CancelledBy.BANK, at);
    }

    /**
     * The originator closes the customer's account on its side and cancels the mandate (an
     * AUDDIS cancellation). Collections still due on it fail with ACCOUNT_CLOSED_BY_ORIGINATOR.
     */
    public void originatorClosedAccount(UUID mandateId, Instant at) {
        cancel(mandateId, CancelledBy.ORIGINATOR, at);
    }

    /** A mandate with no collection for a long time lapses. */
    public void markDormant(UUID mandateId) {
        Mandate mandate = mandate(mandateId);
        if (mandate.getStatus() != MandateStatus.ACTIVE) {
            throw new DomainException("Mandate " + mandateId + " is not active");
        }
        mandate.markDormant();
    }

    /** The originator presents a collection for the due date. */
    public DirectDebitCollection present(UUID mandateId, BigDecimal amount, LocalDate dueDate, Instant at) {
        Mandate mandate = mandate(mandateId);
        if (amount.signum() <= 0) {
            throw new DomainException("Amount must be positive");
        }
        Account account = accounts.account(mandate.getAccountId());
        return collections.save(new DirectDebitCollection(UUID.randomUUID(), mandateId, amount,
                account.getCurrency(), dueDate, at));
    }

    /** Pays or fails a pending collection on (or after) its due date. */
    public DirectDebitCollection process(UUID collectionId, Instant at) {
        DirectDebitCollection collection = collections.findById(collectionId)
                .orElseThrow(() -> new NotFoundException("Collection " + collectionId + " not found"));
        if (collection.getResult() != CollectionResult.PENDING) {
            throw new DomainException("Collection " + collectionId + " was already processed");
        }
        if (collection.getDueDate().isAfter(AccountOperations.day(at))) {
            throw new DomainException("Collection " + collectionId + " is not due until " + collection.getDueDate());
        }
        Mandate mandate = mandate(collection.getMandateId());
        Account account = accounts.account(mandate.getAccountId());
        Optional<CollectionFailureCode> failure = CollectionRules.check(mandate, account, collection.getAmount());
        if (failure.isPresent()) {
            collection.failed(failure.get(), at);
        } else {
            collection.collected(at);
            accounts.book(account, collection.getAmount().negate(), TransactionType.DIRECT_DEBIT,
                    mandate.getOriginator() + " " + mandate.getReference(), collection.getId(), at);
        }
        return collection;
    }

    public Mandate mandate(UUID mandateId) {
        return mandates.findById(mandateId).orElseThrow(() -> new NotFoundException("Mandate " + mandateId + " not found"));
    }

    private void cancel(UUID mandateId, CancelledBy by, Instant at) {
        Mandate mandate = mandate(mandateId);
        if (mandate.getStatus() == MandateStatus.CANCELLED) {
            throw new DomainException("Mandate " + mandateId + " is already cancelled");
        }
        mandate.cancel(by, at);
    }
}
