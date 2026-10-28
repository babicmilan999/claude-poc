package com.claudepoc.banking.api;

import com.claudepoc.banking.domain.DomainException;
import com.claudepoc.banking.domain.NotFoundException;
import com.claudepoc.banking.situations.SituationException;
import org.springframework.beans.TypeMismatchException;
import org.springframework.http.HttpHeaders;
import org.springframework.http.HttpStatus;
import org.springframework.http.HttpStatusCode;
import org.springframework.http.ProblemDetail;
import org.springframework.http.ResponseEntity;
import org.springframework.lang.NonNull;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RestControllerAdvice;
import org.springframework.web.context.request.WebRequest;
import org.springframework.web.method.annotation.MethodArgumentTypeMismatchException;
import org.springframework.web.servlet.mvc.method.annotation.ResponseEntityExceptionHandler;

/**
 * Errors as RFC 9457 problem details (application/problem+json). An id that does not exist,
 * belongs to another customer or is not a UUID is a 404; a bad query value is a 400.
 */
@RestControllerAdvice
public class ApiErrors extends ResponseEntityExceptionHandler {

    @ExceptionHandler(NotFoundException.class)
    public ProblemDetail notFound(NotFoundException e) {
        return problem(HttpStatus.NOT_FOUND, "Not found", e.getMessage());
    }

    @ExceptionHandler(BadRequestException.class)
    public ProblemDetail badRequest(BadRequestException e) {
        return problem(HttpStatus.BAD_REQUEST, "Bad request", e.getMessage());
    }

    /** Simulation requests that the domain cannot set up (bad params, customer in the wrong state). */
    @ExceptionHandler({SituationException.class, DomainException.class})
    public ProblemDetail cannotSetUp(RuntimeException e) {
        return problem(HttpStatus.BAD_REQUEST, "Bad request", e.getMessage());
    }

    @Override
    protected ResponseEntity<Object> handleTypeMismatch(@NonNull TypeMismatchException ex, @NonNull HttpHeaders headers,
                                                        @NonNull HttpStatusCode status, @NonNull WebRequest request) {
        if (ex instanceof MethodArgumentTypeMismatchException m && m.getParameter().hasParameterAnnotation(PathVariable.class)) {
            return ResponseEntity.status(HttpStatus.NOT_FOUND)
                    .body(problem(HttpStatus.NOT_FOUND, "Not found", "No record with id " + ex.getValue()));
        }
        String name = ex instanceof MethodArgumentTypeMismatchException m ? m.getName() : ex.getPropertyName();
        return ResponseEntity.status(HttpStatus.BAD_REQUEST)
                .body(problem(HttpStatus.BAD_REQUEST, "Bad request", "Invalid value for '" + name + "': " + ex.getValue()));
    }

    public static ProblemDetail problem(HttpStatus status, String title, String detail) {
        ProblemDetail problem = ProblemDetail.forStatusAndDetail(status, detail);
        problem.setTitle(title);
        return problem;
    }
}
