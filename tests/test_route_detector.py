from pathlib import Path

from deplar.scanner.route_detector import (
    detect_java_routes,
    detect_python_routes,
    detect_ts_routes,
)

FX = Path(__file__).parent / "fixtures"


def _keys(routes):
    return {f"{r.method} {r.path}" for r in routes}


class TestJavaSpringRoutes:
    def test_controller_prefix_joined(self):
        routes = detect_java_routes(FX / "payment-service/src/PaymentController.java")
        keys = _keys(routes)
        assert "POST /v1/charge" in keys
        assert "GET /v1/payments/{}" in keys

    def test_non_controller_ignored(self, tmp_path):
        f = tmp_path / "Plain.java"
        f.write_text("class Plain { @GetMapping(\"/x\") void x() {} }")
        # no @RestController -> not treated as a provided route
        assert detect_java_routes(f) == []


class TestTsRoutes:
    def test_express_router_routes(self):
        routes = detect_ts_routes(FX / "user-service/src/routes.ts")
        keys = _keys(routes)
        assert "GET /v1/users/{}" in keys
        assert "POST /users" in keys

    def test_axios_client_is_not_a_route(self):
        # user-service/src/client.ts calls axios.get(...) — a *consumer*, not a
        # provided route. It must not be picked up as a route.
        routes = detect_ts_routes(FX / "user-service/src/client.ts")
        assert routes == []

    def test_nest_controller(self, tmp_path):
        f = tmp_path / "cats.controller.ts"
        f.write_text(
            "@Controller('cats')\n"
            "class CatsController {\n"
            "  @Get(':id')\n"
            "  findOne() {}\n"
            "  @Post()\n"
            "  create() {}\n"
            "}\n"
        )
        keys = _keys(detect_ts_routes(f))
        assert "GET /cats/{}" in keys
        assert "POST /cats" in keys


class TestPythonRoutes:
    def test_fastapi(self, tmp_path):
        f = tmp_path / "api.py"
        f.write_text(
            "@app.get('/v1/users/{id}')\n"
            "def get_user(id): ...\n"
            "@router.post('/v1/users')\n"
            "def create_user(): ...\n"
        )
        keys = _keys(detect_python_routes(f))
        assert "GET /v1/users/{}" in keys
        assert "POST /v1/users" in keys

    def test_flask_methods(self, tmp_path):
        f = tmp_path / "app.py"
        f.write_text(
            "@app.route('/charge', methods=['POST'])\n"
            "def charge(): ...\n"
        )
        keys = _keys(detect_python_routes(f))
        assert "POST /charge" in keys
