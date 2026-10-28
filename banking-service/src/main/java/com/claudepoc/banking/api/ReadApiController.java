package com.claudepoc.banking.api;

import com.claudepoc.banking.api.ApiDtos.AccountDto;
import com.claudepoc.banking.api.ApiDtos.BalanceDto;
import com.claudepoc.banking.api.ApiDtos.CardAuthorisationDto;
import com.claudepoc.banking.api.ApiDtos.CardControlsDto;
import com.claudepoc.banking.api.ApiDtos.CardDto;
import com.claudepoc.banking.api.ApiDtos.CustomerDto;
import com.claudepoc.banking.api.ApiDtos.DirectDebitCollectionDto;
import com.claudepoc.banking.api.ApiDtos.FraudFlagDto;
import com.claudepoc.banking.api.ApiDtos.MandateDto;
import com.claudepoc.banking.api.ApiDtos.PayeeDto;
import com.claudepoc.banking.api.ApiDtos.PaymentAttemptDto;
import com.claudepoc.banking.api.ApiDtos.ScheduledPaymentDto;
import com.claudepoc.banking.api.ApiDtos.TransactionDto;
import com.claudepoc.banking.domain.Account;
import com.claudepoc.banking.domain.Card;
import com.claudepoc.banking.domain.Enums;
import com.claudepoc.banking.domain.Enums.AuthorisationResult;
import com.claudepoc.banking.domain.Enums.CollectionResult;
import com.claudepoc.banking.domain.Enums.FraudFlagStatus;
import com.claudepoc.banking.domain.Enums.MandateStatus;
import com.claudepoc.banking.domain.Enums.PaymentAttemptResult;
import com.claudepoc.banking.domain.Money;
import java.time.Clock;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.util.EnumSet;
import java.util.List;
import java.util.Set;
import java.util.UUID;
import org.springframework.format.annotation.DateTimeFormat;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

/** The read-only API of contracts/banking-api.yaml. */
@RestController

public class ReadApiController {

    private static final int DEFAULT_LIMIT = 50;
    private static final int MAX_LIMIT = 200;

    private final BankingQueries queries;
    private final Clock clock;

    public ReadApiController(BankingQueries queries, Clock clock) {
        this.queries = queries;
        this.clock = clock;
    }

    @GetMapping("/customers")
    public List<CustomerDto> listCustomers() {
        return queries.customers().stream().map(ApiMapper::customer).toList();
    }

    @GetMapping("/customers/{customer_id}")
    public CustomerDto getCustomer(@PathVariable("customer_id") UUID customerId) {
        return ApiMapper.customer(queries.customer(customerId));
    }

    @GetMapping("/customers/{customer_id}/accounts")
    public List<AccountDto> listAccounts(@PathVariable("customer_id") UUID customerId) {
        return queries.accounts(customerId).stream().map(ApiMapper::account).toList();
    }

    @GetMapping("/customers/{customer_id}/accounts/{account_id}/balance")
    public BalanceDto getAccountBalance(@PathVariable("customer_id") UUID customerId,
                                        @PathVariable("account_id") UUID accountId) {
        Account a = queries.account(customerId, accountId);
        return new BalanceDto(a.getId(), a.getCurrency(), Money.format(a.getBalance()), Money.format(a.available()),
                Money.format(a.getOverdraftLimit()), Instant.now(clock));
    }

    @GetMapping("/customers/{customer_id}/accounts/{account_id}/transactions")
    public List<TransactionDto> listTransactions(
            @PathVariable("customer_id") UUID customerId, @PathVariable("account_id") UUID accountId,
            @RequestParam(name = "since", required = false) @DateTimeFormat(iso = DateTimeFormat.ISO.DATE_TIME) OffsetDateTime since,
            @RequestParam(name = "until", required = false) @DateTimeFormat(iso = DateTimeFormat.ISO.DATE_TIME) OffsetDateTime until,
            @RequestParam(name = "limit", required = false) Integer limit) {
        return queries.transactions(customerId, accountId, instant(since), instant(until), limit(limit)).stream()
                .map(ApiMapper::transaction).toList();
    }

    @GetMapping("/customers/{customer_id}/cards")
    public List<CardDto> listCards(@PathVariable("customer_id") UUID customerId) {
        return queries.cards(customerId).stream().map(ApiMapper::card).toList();
    }

    @GetMapping("/customers/{customer_id}/cards/{card_id}/controls")
    public CardControlsDto getCardControls(@PathVariable("customer_id") UUID customerId,
                                           @PathVariable("card_id") UUID cardId) {
        Card c = queries.card(customerId, cardId);
        return new CardControlsDto(c.getId(), Money.format(c.getContactlessLimit()), c.getBlockedMerchantCategories(),
                c.isOnlinePaymentsEnabled());
    }

    @GetMapping("/customers/{customer_id}/authorisations")
    public List<CardAuthorisationDto> listCardAuthorisations(
            @PathVariable("customer_id") UUID customerId,
            @RequestParam(name = "card_id", required = false) UUID cardId,
            @RequestParam(name = "result", required = false) String result,
            @RequestParam(name = "since", required = false) @DateTimeFormat(iso = DateTimeFormat.ISO.DATE_TIME) OffsetDateTime since,
            @RequestParam(name = "limit", required = false) Integer limit) {
        return queries.authorisations(customerId, cardId, parse(AuthorisationResult.class, "result", result),
                instant(since), limit(limit)).stream().map(ApiMapper::authorisation).toList();
    }

    @GetMapping("/customers/{customer_id}/fraud-flags")
    public List<FraudFlagDto> listFraudFlags(@PathVariable("customer_id") UUID customerId,
                                             @RequestParam(name = "card_id", required = false) UUID cardId,
                                             @RequestParam(name = "status", required = false) String status) {
        return queries.fraudFlags(customerId, cardId, parse(FraudFlagStatus.class, "status", status)).stream()
                .map(ApiMapper::fraudFlag).toList();
    }

    @GetMapping("/customers/{customer_id}/scheduled-payments")
    public List<ScheduledPaymentDto> listScheduledPayments(@PathVariable("customer_id") UUID customerId) {
        return queries.scheduledPayments(customerId).stream().map(ApiMapper::scheduledPayment).toList();
    }

    @GetMapping("/customers/{customer_id}/payment-attempts")
    public List<PaymentAttemptDto> listPaymentAttempts(
            @PathVariable("customer_id") UUID customerId,
            @RequestParam(name = "scheduled_payment_id", required = false) UUID scheduledPaymentId,
            @RequestParam(name = "result", required = false) List<String> result,
            @RequestParam(name = "since", required = false) @DateTimeFormat(iso = DateTimeFormat.ISO.DATE_TIME) OffsetDateTime since,
            @RequestParam(name = "limit", required = false) Integer limit) {
        Set<PaymentAttemptResult> results = EnumSet.noneOf(PaymentAttemptResult.class);
        if (result != null) {
            for (String r : result) {
                if (!r.isBlank()) {
                    results.add(parse(PaymentAttemptResult.class, "result", r));
                }
            }
        }
        return queries.paymentAttempts(customerId, scheduledPaymentId, results, instant(since), limit(limit)).stream()
                .map(ApiMapper::paymentAttempt).toList();
    }

    @GetMapping("/customers/{customer_id}/payees")
    public List<PayeeDto> listPayees(@PathVariable("customer_id") UUID customerId) {
        return queries.payees(customerId).stream().map(ApiMapper::payee).toList();
    }

    @GetMapping("/customers/{customer_id}/payees/{payee_id}")
    public PayeeDto getPayee(@PathVariable("customer_id") UUID customerId, @PathVariable("payee_id") UUID payeeId) {
        return ApiMapper.payee(queries.payee(customerId, payeeId));
    }

    @GetMapping("/customers/{customer_id}/mandates")
    public List<MandateDto> listMandates(@PathVariable("customer_id") UUID customerId,
                                         @RequestParam(name = "status", required = false) String status) {
        return queries.mandates(customerId, parse(MandateStatus.class, "status", status)).stream()
                .map(ApiMapper::mandate).toList();
    }

    @GetMapping("/customers/{customer_id}/direct-debit-collections")
    public List<DirectDebitCollectionDto> listDirectDebitCollections(
            @PathVariable("customer_id") UUID customerId,
            @RequestParam(name = "mandate_id", required = false) UUID mandateId,
            @RequestParam(name = "result", required = false) String result,
            @RequestParam(name = "since", required = false) @DateTimeFormat(iso = DateTimeFormat.ISO.DATE_TIME) OffsetDateTime since,
            @RequestParam(name = "limit", required = false) Integer limit) {
        return queries.collections(customerId, mandateId, parse(CollectionResult.class, "result", result),
                instant(since), limit(limit)).stream().map(ApiMapper::collection).toList();
    }

    private static int limit(Integer limit) {
        if (limit == null) {
            return DEFAULT_LIMIT;
        }
        if (limit < 1 || limit > MAX_LIMIT) {
            throw new BadRequestException("limit must be between 1 and " + MAX_LIMIT);
        }
        return limit;
    }

    private static Instant instant(OffsetDateTime value) {
        return value == null ? null : value.toInstant();
    }

    private static <E extends Enum<E> & Enums.Wire> E parse(Class<E> type, String name, String value) {
        if (value == null || value.isBlank()) {
            return null;
        }
        E parsed = Enums.fromWire(type, value);
        if (parsed == null) {
            throw new BadRequestException("Invalid value for '" + name + "': " + value);
        }
        return parsed;
    }
}
