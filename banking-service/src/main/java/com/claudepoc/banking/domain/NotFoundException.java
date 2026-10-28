package com.claudepoc.banking.domain;

/** A record does not exist, or is not owned by the customer in the path. */
public class NotFoundException extends RuntimeException {
    public NotFoundException(String message) {
        super(message);
    }
}
