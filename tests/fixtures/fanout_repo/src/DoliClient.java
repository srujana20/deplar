package com.airline.pss;

import org.springframework.web.client.RestTemplate;

public class DoliClient {

    private RestTemplate restTemplate;

    public Pnr postCreate(PnrRequest req) {
        return restTemplate.postForObject("https://doli.ek.com/create-pnr", req, Pnr.class);
    }
}
