package com.claudepoc.banking.api;

import com.claudepoc.banking.api.ApiDtos.AccountDto;
import com.claudepoc.banking.api.ApiDtos.CardAuthorisationDto;
import com.claudepoc.banking.api.ApiDtos.CardDto;
import com.claudepoc.banking.api.ApiDtos.CustomerDto;
import com.claudepoc.banking.api.ApiDtos.DirectDebitCollectionDto;
import com.claudepoc.banking.api.ApiDtos.FraudFlagDto;
import com.claudepoc.banking.api.ApiDtos.MandateDto;
import com.claudepoc.banking.api.ApiDtos.PayeeDto;
import com.claudepoc.banking.api.ApiDtos.PaymentAttemptDto;
import com.claudepoc.banking.api.ApiDtos.ScheduledPaymentDto;
import com.claudepoc.banking.api.ApiDtos.TransactionDto;
import com.claudepoc.banking.api.BankingQueries.CollectionView;
import com.claudepoc.banking.api.BankingQueries.ScheduledPaymentView;
import com.claudepoc.banking.domain.Account;
import com.claudepoc.banking.domain.Card;
import com.claudepoc.banking.domain.CardAuthorisation;
import com.claudepoc.banking.domain.Customer;
import com.claudepoc.banking.domain.DirectDebitCollection;
import com.claudepoc.banking.domain.Enums.Wire;
import com.claudepoc.banking.domain.FraudFlag;
import com.claudepoc.banking.domain.Mandate;
import com.claudepoc.banking.domain.Money;
import com.claudepoc.banking.domain.Payee;
import com.claudepoc.banking.domain.PaymentAttempt;
import com.claudepoc.banking.domain.ScheduledPayment;
import com.claudepoc.banking.domain.Transaction;

/** Entity to response mapping (contract values for enums, two-decimal money strings). */
final class ApiMapper {

    private ApiMapper() {
    }

    static CustomerDto customer(Customer c) {
        return new CustomerDto(c.getId(), c.getName(), c.getEmail(), wire(c.getStatus()), c.isVulnerable(), c.getCreatedAt());
    }

    static AccountDto account(Account a) {
        return new AccountDto(a.getId(), a.getCustomerId(), a.getName(), a.getSortCode(), a.getAccountNumber(),
                a.getCurrency(), Money.format(a.getBalance()), Money.format(a.getOverdraftLimit()), wire(a.getStatus()));
    }

    static TransactionDto transaction(Transaction t) {
        return new TransactionDto(t.getId(), t.getAccountId(), Money.format(t.getAmount()), t.getCurrency(),
                wire(t.getType()), t.getDescription(), t.getReferenceId(), t.getBookedAt());
    }

    static CardDto card(Card c) {
        return new CardDto(c.getId(), c.getAccountId(), c.getLast4(), wire(c.getBrand()), wire(c.getStatus()),
                wire(c.getStatusReason()), c.getExpiry());
    }

    static CardAuthorisationDto authorisation(CardAuthorisation a) {
        return new CardAuthorisationDto(a.getId(), a.getCardId(), Money.format(a.getAmount()), a.getCurrency(),
                a.getMerchantName(), a.getMerchantMcc(), wire(a.getChannel()), wire(a.getResult()),
                wire(a.getDeclineReason()), a.getFraudRuleId(), a.getCreatedAt());
    }

    static FraudFlagDto fraudFlag(FraudFlag f) {
        return new FraudFlagDto(f.getId(), f.getCustomerId(), f.getCardId(), f.getRuleId(), f.getDescription(),
                f.getRiskScore(), wire(f.getStatus()), wire(f.getAction()), f.getCreatedAt(), f.getClearedAt());
    }

    static ScheduledPaymentDto scheduledPayment(ScheduledPaymentView v) {
        ScheduledPayment p = v.payment();
        return new ScheduledPaymentDto(p.getId(), p.getAccountId(), p.getPayeeId(),
                v.payee() == null ? null : v.payee().getName(), p.getReference(), Money.format(p.getAmount()),
                p.getCurrency(), wire(p.getFrequency()), p.getNextRun(), wire(p.getStatus()),
                v.lastAttempt() == null ? null : paymentAttempt(v.lastAttempt()));
    }

    static PaymentAttemptDto paymentAttempt(PaymentAttempt a) {
        return new PaymentAttemptDto(a.getId(), a.getScheduledPaymentId(), Money.format(a.getAmount()),
                wire(a.getResult()), wire(a.getFailureReason()), a.getRetryCount(), a.getAttemptedAt());
    }

    static PayeeDto payee(Payee p) {
        return new PayeeDto(p.getId(), p.getCustomerId(), p.getName(), p.getSortCode(), p.getAccountNumber(),
                wire(p.getAccountStatus()));
    }

    static MandateDto mandate(Mandate m) {
        return new MandateDto(m.getId(), m.getAccountId(), m.getOriginator(), m.getReference(), wire(m.getStatus()),
                wire(m.getCancelledBy()), m.getCreatedAt(), m.getCancelledAt());
    }

    static DirectDebitCollectionDto collection(CollectionView v) {
        DirectDebitCollection c = v.collection();
        return new DirectDebitCollectionDto(c.getId(), c.getMandateId(), v.mandate().getOriginator(),
                Money.format(c.getAmount()), c.getCurrency(), c.getDueDate(), wire(c.getResult()),
                wire(c.getFailureCode()), c.getProcessedAt());
    }

    private static String wire(Wire value) {
        return value == null ? null : value.wire();
    }
}
