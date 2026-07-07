package com.company.payments;

import org.springframework.web.client.RestTemplate;
import org.springframework.ws.client.core.WebServiceTemplate;

public class UserSyncService {

    private static final String USERS_URL = "https://user-service.internal";
    private RestTemplate restTemplate;
    private WebServiceTemplate webServiceTemplate;

    // Non-Feign HTTP client (RestTemplate) hitting a route user-service provides.
    public User createUser(UserRequest req) {
        return restTemplate.postForObject(USERS_URL + "/users", req, User.class);
    }

    // SOAP call to an external legacy billing system.
    public BillingResult bill(BillingRequest req) {
        return (BillingResult) webServiceTemplate.marshalSendAndReceive(
            "https://legacy-billing.internal/ws/billing", req);
    }
}
