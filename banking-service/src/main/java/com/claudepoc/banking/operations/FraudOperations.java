package com.claudepoc.banking.operations;

import com.claudepoc.banking.domain.Card;
import com.claudepoc.banking.domain.DomainException;
import com.claudepoc.banking.domain.Enums.CardStatus;
import com.claudepoc.banking.domain.Enums.CardStatusReason;
import com.claudepoc.banking.domain.Enums.FraudFlagStatus;
import com.claudepoc.banking.domain.FraudFlag;
import com.claudepoc.banking.domain.NotFoundException;
import com.claudepoc.banking.domain.Repositories.FraudFlagRepository;
import java.time.Instant;
import java.util.UUID;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

/** What the fraud team does with a flag. Clearing a flag unblocks the card it blocked. */
@Service
@Transactional
public class FraudOperations {

    private final FraudFlagRepository flags;
    private final CardOperations cards;

    public FraudOperations(FraudFlagRepository flags, CardOperations cards) {
        this.flags = flags;
        this.cards = cards;
    }

    public void clear(UUID flagId, Instant at) {
        FraudFlag flag = flag(flagId);
        if (flag.getStatus() != FraudFlagStatus.OPEN) {
            throw new DomainException("Fraud flag " + flagId + " is not open");
        }
        flag.resolve(FraudFlagStatus.CLEARED, at);
        if (flag.getCardId() != null) {
            Card card = cards.card(flag.getCardId());
            if (card.getStatus() == CardStatus.BLOCKED && card.getStatusReason() == CardStatusReason.FRAUD_SUSPECTED) {
                card.changeStatus(CardStatus.ACTIVE, null);
            }
        }
    }

    public void confirmFraud(UUID flagId, Instant at) {
        flag(flagId).resolve(FraudFlagStatus.CONFIRMED_FRAUD, at);
    }

    private FraudFlag flag(UUID flagId) {
        return flags.findById(flagId).orElseThrow(() -> new NotFoundException("Fraud flag " + flagId + " not found"));
    }
}
