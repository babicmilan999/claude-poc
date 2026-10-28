package com.claudepoc.banking.contract;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.util.Map;
import org.springframework.http.MediaType;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.MvcResult;
import org.springframework.test.web.servlet.request.MockHttpServletRequestBuilder;
import org.springframework.test.web.servlet.request.MockMvcRequestBuilders;

/** Calls the service through MockMvc and checks every response against the contract. */
public final class ContractClient {

    public record Response(int status, String contentType, String body, JsonNode json) {
    }

    private static final ObjectMapper JSON = new ObjectMapper();
    private final MockMvc mvc;
    private final ContractValidator contract = ContractValidator.get();

    public ContractClient(MockMvc mvc) {
        this.mvc = mvc;
    }

    /**
     * GET a path template such as "/customers/{customer_id}" with path variables and an optional
     * query string, and validate the response against that operation in the contract.
     */
    public Response get(String template, Map<String, Object> vars, String query) throws Exception {
        String uri = expand(template, vars) + (query == null || query.isEmpty() ? "" : "?" + query);
        return validate("GET", template, MockMvcRequestBuilders.get(uri).accept(MediaType.APPLICATION_JSON, MediaType.APPLICATION_PROBLEM_JSON));
    }

    public Response get(String template, Map<String, Object> vars) throws Exception {
        return get(template, vars, null);
    }

    public Response post(String template, String jsonBody) throws Exception {
        MockHttpServletRequestBuilder request = MockMvcRequestBuilders.post(template)
                .accept(MediaType.APPLICATION_JSON, MediaType.APPLICATION_PROBLEM_JSON);
        if (jsonBody != null) {
            request.contentType(MediaType.APPLICATION_JSON).content(jsonBody);
        }
        return validate("POST", template, request);
    }

    private Response validate(String method, String template, MockHttpServletRequestBuilder request) throws Exception {
        MvcResult result = mvc.perform(request).andReturn();
        int status = result.getResponse().getStatus();
        String body = result.getResponse().getContentAsString(java.nio.charset.StandardCharsets.UTF_8);
        String contentType = result.getResponse().getContentType();
        contract.assertResponse(method, template, status, contentType, body);
        return new Response(status, contentType, body, body.isEmpty() ? null : JSON.readTree(body));
    }

    static String expand(String template, Map<String, Object> vars) {
        String uri = template;
        for (Map.Entry<String, Object> e : vars.entrySet()) {
            uri = uri.replace("{" + e.getKey() + "}", String.valueOf(e.getValue()));
        }
        return uri;
    }
}
