package com.claudepoc.banking.operations;

import com.claudepoc.banking.domain.Account;
import com.claudepoc.banking.domain.Card;
import com.claudepoc.banking.domain.CardAuthorisation;
import com.claudepoc.banking.domain.DomainException;
import com.claudepoc.banking.domain.Enums.AuthorisationResult;
import com.claudepoc.banking.domain.Enums.CardStatus;
import com.claudepoc.banking.domain.Enums.CardStatusReason;
import com.claudepoc.banking.domain.Enums.Channel;
import com.claudepoc.banking.domain.Enums.DeclineReason;
import com.claudepoc.banking.domain.Enums.FraudAction;
import com.claudepoc.banking.domain.Enums.TransactionType;
import com.claudepoc.banking.domain.FraudFlag;
import com.claudepoc.banking.domain.Money;
import com.claudepoc.banking.domain.Repositories.CardAuthorisationRepository;
import com.claudepoc.banking.domain.Repositories.FraudFlagRepository;
import com.claudepoc.banking.rules.AuthorisationRules;
import com.claudepoc.banking.rules.FraudRules;
import com.claudepoc.banking.rules.FraudRules.FraudHit;
import java.math.BigDecimal;
import java.time.Instant;
import java.time.LocalDate;
import java.util.Optional;
import java.util.UUID;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

/**
 * Card payment authorisation: runs the rules, records the authorisation, and books approved
 * payments straight away (so nothing stays pending). A fraud hit raises a fraud flag and blocks the card.
 */
@Service
@Transactional
public class AuthorisationOperations {

    public record AuthorisationRequest(UUID cardId, BigDecimal amount, String merchantName, String merchantMcc,
                                       Channel channel, Instant at) {
    }

    private final CardOperations cards;
    private final AccountOperations accounts;
    private final CardAuthorisationRepository authorisations;
    private final FraudFlagRepository fraudFlags;

    public AuthorisationOperations(CardOperations cards, AccountOperations accounts,
                                   CardAuthorisationRepository authorisations, FraudFlagRepository fraudFlags) {
        this.cards = cards;
        this.accounts = accounts;
        this.authorisations = authorisations;
        this.fraudFlags = fraudFlags;
    }

    public CardAuthorisation authorise(AuthorisationRequest request) {
        if (request.amount().signum() <= 0) {
            throw new DomainException("Amount must be positive");
        }
        CardOperations.requireMcc(request.merchantMcc());
        BigDecimal amount = Money.normalise(request.amount());
        Card card = cards.card(request.cardId());
        Account account = accounts.account(card.getAccountId());
        LocalDate day = AccountOperations.day(request.at());

        if (card.getStatus() != CardStatus.EXPIRED && card.isPastExpiry(day)) {
            card.changeStatus(CardStatus.EXPIRED, CardStatusReason.EXPIRED);
        }

        Optional<DeclineReason> decline = AuthorisationRules.cardChecks(account, card, amount,
                request.merchantMcc(), request.channel(), day);
        FraudHit fraudHit = null;
        if (decline.isEmpty()) {
            long recent = authorisations.countByCardIdAndCreatedAtBetween(card.getId(),
                    request.at().minus(FraudRules.VELOCITY_WINDOW), request.at());
            Optional<FraudHit> hit = FraudRules.evaluate(amount, request.merchantMcc(), request.channel(), recent);
            if (hit.isPresent()) {
                fraudHit = hit.get();
                decline = Optional.of(DeclineReason.FRAUD_RULE_TRIGGERED);
            }
        }
        if (decline.isEmpty()) {
            decline = AuthorisationRules.fundsCheck(account, amount);
        }

        CardAuthorisation authorisation = authorisations.save(new CardAuthorisation(UUID.randomUUID(), card.getId(),
                amount, account.getCurrency(), request.merchantName(), request.merchantMcc(), request.channel(),
                decline.isPresent() ? AuthorisationResult.DECLINED : AuthorisationResult.APPROVED,
                decline.orElse(null), fraudHit == null ? null : fraudHit.ruleId(), request.at()));

        if (fraudHit != null) {
            fraudFlags.save(new FraudFlag(UUID.randomUUID(), account.getCustomerId(), card.getId(), fraudHit.ruleId(),
                    fraudHit.description(), fraudHit.riskScore(), fraudHit.action(), request.at()));
            if (fraudHit.action() == FraudAction.CARD_BLOCKED) {
                cards.blockForSuspectedFraud(card.getId());
            }
        }
        if (decline.isEmpty()) {
            accounts.book(account, amount.negate(), TransactionType.CARD_PAYMENT, request.merchantName(),
                    authorisation.getId(), request.at());
        }
        return authorisation;
    }
}
