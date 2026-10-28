package com.claudepoc.banking.contract;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.fail;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.JsonNodeFactory;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.fasterxml.jackson.dataformat.yaml.YAMLMapper;
import com.networknt.schema.JsonSchema;
import com.networknt.schema.JsonSchemaFactory;
import com.networknt.schema.SchemaValidatorsConfig;
import com.networknt.schema.SpecVersion;
import com.networknt.schema.ValidationMessage;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Iterator;
import java.util.Map;
import java.util.Set;
import java.util.TreeSet;
import java.util.concurrent.ConcurrentHashMap;

/**
 * Validates HTTP responses against contracts/banking-api.yaml (OpenAPI 3.1, whose schemas are
 * JSON Schema 2020-12). The `./scenario.schema.json#/$defs/...` refs are inlined. Stricter than
 * the contract in one way: objects may not carry properties the contract does not declare, so
 * an extra field (for example an answer key leaking into the read API) fails the test.
 */
public final class ContractValidator {

    private static final ObjectMapper JSON = new ObjectMapper();
    private static ContractValidator instance;

    private final JsonNode spec;
    private final ObjectNode root;
    private final JsonSchemaFactory factory = JsonSchemaFactory.getInstance(SpecVersion.VersionFlag.V202012);
    private final SchemaValidatorsConfig config = SchemaValidatorsConfig.builder().formatAssertionsEnabled(true).build();
    private final Map<String, JsonSchema> cache = new ConcurrentHashMap<>();
    private final Set<String> covered = ConcurrentHashMap.newKeySet();

    private ContractValidator(Path contractsDir) throws IOException {
        JsonNode api = new YAMLMapper().readTree(contractsDir.resolve("banking-api.yaml").toFile());
        JsonNode scenario = JSON.readTree(Files.readString(contractsDir.resolve("scenario.schema.json")));
        rewriteRefs(api);
        this.spec = api;
        this.root = JsonNodeFactory.instance.objectNode();
        root.put("$schema", "https://json-schema.org/draft/2020-12/schema");
        root.set("$defs", scenario.get("$defs").deepCopy());
        ObjectNode components = root.putObject("components");
        components.set("schemas", api.get("components").get("schemas").deepCopy());
        // RFC 9457 allows extension members in problem details, so Problem stays open.
        ((ObjectNode) components.get("schemas").get("Problem")).put("additionalProperties", true);
        forbidUndeclaredProperties(components.get("schemas"));
    }

    public static synchronized ContractValidator get() {
        if (instance == null) {
            String dir = System.getProperty("contracts.dir", "../contracts");
            try {
                instance = new ContractValidator(Path.of(dir));
            } catch (IOException e) {
                throw new IllegalStateException("Cannot read the contracts in " + dir, e);
            }
        }
        return instance;
    }

    /** Every operation in the contract, as "METHOD /path/template". */
    public Set<String> operations() {
        Set<String> ops = new TreeSet<>();
        spec.get("paths").fields().forEachRemaining(path -> path.getValue().fieldNames().forEachRemaining(method -> {
            if (Set.of("get", "post", "put", "patch", "delete").contains(method)) {
                ops.add(method.toUpperCase() + " " + path.getKey());
            }
        }));
        return ops;
    }

    /** Operations validated at least once, by status: "GET /customers 200". */
    public Set<String> covered() {
        return Set.copyOf(covered);
    }

    /** Fails unless the status is declared for the operation and the body matches its schema. */
    public void assertResponse(String method, String pathTemplate, int status, String contentType, String body) {
        JsonNode operation = spec.path("paths").path(pathTemplate).path(method.toLowerCase());
        if (operation.isMissingNode()) {
            fail("Operation not in the contract: " + method + " " + pathTemplate);
        }
        JsonNode response = operation.path("responses").path(String.valueOf(status));
        if (response.isMissingNode()) {
            fail(method + " " + pathTemplate + " returned " + status + ", which the contract does not declare. Body: " + body);
        }
        response = resolve(response);
        JsonNode content = response.path("content");
        if (content.isMissingNode() || content.isEmpty()) {
            assertThat(body).as("body of %s %s %s", method, pathTemplate, status).isEmpty();
        } else {
            String mediaType = contentType == null ? "" : contentType.split(";")[0].trim();
            JsonNode media = content.path(mediaType);
            if (media.isMissingNode()) {
                fail(method + " " + pathTemplate + " " + status + " returned content type '" + mediaType
                        + "'; the contract declares " + iterable(content.fieldNames()));
            }
            assertMatches(schema(method + " " + pathTemplate + " " + status + " " + mediaType, media.get("schema")),
                    body, method + " " + pathTemplate + " " + status);
        }
        covered.add(method.toUpperCase() + " " + pathTemplate + " " + status);
    }

    /** Validates a JSON document against a named component schema. */
    public void assertMatchesSchema(String componentSchema, String body) {
        ObjectNode ref = JsonNodeFactory.instance.objectNode().put("$ref", "#/components/schemas/" + componentSchema);
        assertMatches(schema("component " + componentSchema, ref), body, componentSchema);
    }

    private void assertMatches(JsonSchema schema, String body, String what) {
        JsonNode node;
        try {
            node = JSON.readTree(body);
        } catch (IOException e) {
            throw new AssertionError(what + ": body is not JSON: " + body, e);
        }
        Set<ValidationMessage> errors = schema.validate(node);
        if (!errors.isEmpty()) {
            fail(what + " does not match the contract:\n  " + String.join("\n  ", errors.stream().map(ValidationMessage::toString).toList())
                    + "\nBody: " + abbreviate(body));
        }
    }

    private JsonSchema schema(String key, JsonNode schemaNode) {
        return cache.computeIfAbsent(key, k -> {
            ObjectNode doc = root.deepCopy();
            doc.setAll((ObjectNode) schemaNode.deepCopy());
            return factory.getSchema(doc, config);
        });
    }

    private JsonNode resolve(JsonNode node) {
        JsonNode current = node;
        while (current.has("$ref")) {
            String ref = current.get("$ref").asText();
            if (!ref.startsWith("#/")) {
                fail("Unsupported $ref " + ref);
            }
            current = spec.at(ref.substring(1));
        }
        return current;
    }

    private static void rewriteRefs(JsonNode node) {
        if (node instanceof ObjectNode obj) {
            JsonNode ref = obj.get("$ref");
            if (ref != null && ref.isTextual() && ref.asText().startsWith("./scenario.schema.json#")) {
                obj.put("$ref", ref.asText().substring("./scenario.schema.json".length()));
            }
            obj.elements().forEachRemaining(ContractValidator::rewriteRefs);
        } else if (node instanceof ArrayNode arr) {
            arr.elements().forEachRemaining(ContractValidator::rewriteRefs);
        }
    }

    private static void forbidUndeclaredProperties(JsonNode node) {
        if (node instanceof ObjectNode obj) {
            if (obj.has("properties") && !obj.has("additionalProperties")) {
                obj.put("additionalProperties", false);
            }
            obj.elements().forEachRemaining(ContractValidator::forbidUndeclaredProperties);
        } else if (node instanceof ArrayNode arr) {
            arr.elements().forEachRemaining(ContractValidator::forbidUndeclaredProperties);
        }
    }

    private static String iterable(Iterator<String> it) {
        StringBuilder sb = new StringBuilder();
        it.forEachRemaining(s -> sb.append(sb.isEmpty() ? "" : ", ").append(s));
        return sb.toString();
    }

    private static String abbreviate(String s) {
        return s.length() > 2000 ? s.substring(0, 2000) + "..." : s;
    }
}
