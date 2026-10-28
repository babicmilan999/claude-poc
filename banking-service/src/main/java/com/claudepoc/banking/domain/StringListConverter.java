package com.claudepoc.banking.domain;

import jakarta.persistence.AttributeConverter;
import jakarta.persistence.Converter;
import java.util.Arrays;
import java.util.List;

/** Stores a short list of codes as a comma-separated column (portable across PostgreSQL and H2). */
@Converter
public class StringListConverter implements AttributeConverter<List<String>, String> {

    @Override
    public String convertToDatabaseColumn(List<String> values) {
        return values == null ? "" : String.join(",", values);
    }

    @Override
    public List<String> convertToEntityAttribute(String column) {
        if (column == null || column.isBlank()) {
            return new java.util.ArrayList<>();
        }
        return new java.util.ArrayList<>(Arrays.stream(column.split(",")).map(String::trim).filter(s -> !s.isEmpty()).toList());
    }
}
