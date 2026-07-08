package com.airline.pss;

import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestBody;

@RestController
public class PnrController {

    private PnrService pnrService;

    // fans out (via service -> client) to an external API
    @PostMapping("/create-pnr")
    public Pnr createPnr(@RequestBody PnrRequest req) {
        return pnrService.createPnrRecord(req);
    }

    // pure local read — no external fan-out
    @GetMapping("/pnr/{id}")
    public Pnr getPnr(@PathVariable String id) {
        return pnrService.lookup(id);
    }
}
