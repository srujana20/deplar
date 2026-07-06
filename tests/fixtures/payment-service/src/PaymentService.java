import com.company.fraud.FraudClient;
import com.company.users.UserServiceClient;
import org.springframework.web.bind.annotation.RestController;

@FeignClient(name="fraud-service", url="${FRAUD_SERVICE_URL}")
public interface FraudClient {
    @PostMapping("/v1/check")
    FraudResult check(@RequestBody PaymentRequest req);
}

@FeignClient(name="user-service", url="https://user-service.internal")
public interface UserServiceClient {
    @GetMapping("/v1/users/{id}")
    User getUser(@PathVariable String id);
}

public class PaymentService {
    private FraudClient fraudClient;
    private UserServiceClient userClient;
}
