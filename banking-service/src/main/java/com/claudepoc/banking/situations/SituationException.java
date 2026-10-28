package com.claudepoc.banking.situations;

/** A situation cannot be set up (bad parameter, or the customer's state prevents the expected outcome). */
public class SituationException extends RuntimeException {
    public SituationException(String message) {
        super(message);
    }
}
