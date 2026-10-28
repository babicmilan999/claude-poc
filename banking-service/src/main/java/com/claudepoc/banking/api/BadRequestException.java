package com.claudepoc.banking.api;

/** The request is invalid (bad query value, unknown enum, malformed body). Mapped to 400. */
public class BadRequestException extends RuntimeException {
    public BadRequestException(String message) {
        super(message);
    }
}
