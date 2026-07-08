package com.airline.pss;

import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestBody;

@RestController
public class PnrController {

    // Declared as /create-pnr; the real path is /pss-api/create-pnr once the
    // server.servlet.context-path from application.properties is applied.
    @PostMapping("/create-pnr")
    public Pnr createPnr(@RequestBody PnrRequest req) {
        return build(req);
    }

    @GetMapping("/pnr/{id}")
    public Pnr getPnr(@PathVariable String id) {
        return lookup(id);
    }
}
