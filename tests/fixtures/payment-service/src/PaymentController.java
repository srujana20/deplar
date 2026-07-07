package com.company.payments;

import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestBody;

@RestController
@RequestMapping("/v1")
public class PaymentController {

    @PostMapping("/charge")
    public ChargeResult charge(@RequestBody PaymentRequest req) {
        return process(req);
    }

    @GetMapping("/payments/{id}")
    public Payment getPayment(@PathVariable String id) {
        return lookup(id);
    }
}
