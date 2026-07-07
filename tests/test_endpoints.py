from deplar.scanner.endpoints import endpoint_key, normalize_path, split_host_path


class TestNormalizePath:
    def test_spring_brace_param(self):
        assert normalize_path("/v1/orders/{id}") == "/v1/orders/{}"

    def test_express_colon_param(self):
        assert normalize_path("/v1/orders/:orderId/items") == "/v1/orders/{}/items"

    def test_template_literal_hole(self):
        assert normalize_path("/v1/orders/${id}") == "/v1/orders/{}"

    def test_flask_converter(self):
        assert normalize_path("/users/<int:id>") == "/users/{}"

    def test_numeric_segment_folded(self):
        assert normalize_path("orders/123") == "/orders/{}"

    def test_uuid_folded(self):
        assert normalize_path("/o/9f8b1c2d3e4a5b6c") == "/o/{}"

    def test_lowercased_and_leading_slash(self):
        assert normalize_path("V1/Orders") == "/v1/orders"

    def test_empty_is_root(self):
        assert normalize_path("") == "/"

    def test_full_url_stripped_to_path(self):
        assert normalize_path("https://x.internal/v1/charge") == "/v1/charge"

    def test_query_and_fragment_dropped(self):
        assert normalize_path("/search?q=1#top") == "/search"

    def test_two_forms_converge(self):
        assert normalize_path("/v1/users/{id}") == normalize_path("/v1/users/:id")


class TestEndpointKey:
    def test_verb_uppercased(self):
        assert endpoint_key("get", "/v1/users/{id}") == "GET /v1/users/{}"

    def test_empty_method_is_any(self):
        assert endpoint_key("", "/x") == "ANY /x"


class TestSplitHostPath:
    def test_https_url(self):
        assert split_host_path("https://payments.internal/v1/charge") == (
            "payments.internal", "/v1/charge")

    def test_env_wrapper_stripped(self):
        host, path = split_host_path("$ENV:PAYMENTS_URL")
        assert path  # does not crash; env name is not a host

    def test_bare_path(self):
        assert split_host_path("/v1/charge") == ("", "/v1/charge")

    def test_host_only_root_path(self):
        assert split_host_path("https://payments.internal") == (
            "payments.internal", "/")
