import com.company.payments.PaymentsClient;
import com.company.users.UserService;

@FeignClient(name="payments-service", url="${PAYMENTS_URL}")
public interface PaymentsClient {
    @GetMapping("/v1/charge")
    String charge(@RequestBody ChargeRequest req);
}

public class OrderService {
    private PaymentsClient payments;
}