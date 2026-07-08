package com.airline.pss;

public class PnrService {

    private DoliClient doliClient;

    public Pnr createPnrRecord(PnrRequest req) {
        validate(req);
        return doliClient.postCreate(req);
    }

    public Pnr lookup(String id) {
        return repository.find(id);
    }
}
