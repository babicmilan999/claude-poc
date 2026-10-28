package com.claudepoc.banking.domain;

/** A domain operation was called in a state where it is not allowed (programming or setup error). */
public class DomainException extends RuntimeException {
    public DomainException(String message) {
        super(message);
    }
}
