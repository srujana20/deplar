"""Value-holder imports must never become dependencies.

A constants / enum / DTO / config / type import is not a callable service. The
tricky case is one whose name overlaps a real repo (`com.airline.pss.PnrConstants`
in `baggage`): the reconciler would otherwise bind it to the `pss` repo and list
it as a dependency. The resolver drops these up front, in every scan mode.
"""
from pathlib import Path

from deplar.scanner.ast_parser import ImportEdge
from deplar.scanner.reconciler import AliasCatalog, Reconciler
from deplar.scanner.resolver import DependencyResolver, _is_value_holder


def _imp(module):
    return ImportEdge(source_file=Path("X.java"), imported_module=module,
                      imported_names=[module.split(".")[-1]], line_number=1, raw="")


class TestIsValueHolder:
    def test_java_class_names(self):
        assert _is_value_holder("com.airline.pss.PnrConstants")
        assert _is_value_holder("com.airline.pss.constants.AppConstants")
        assert _is_value_holder("com.company.orders.OrderDto")
        assert _is_value_holder("com.company.orders.OrderEntity")
        assert _is_value_holder("com.company.AppConfig")
        assert _is_value_holder("com.company.OrderStatusEnum")
        assert _is_value_holder("com.company.PaymentException")

    def test_python_and_ts_modules(self):
        assert _is_value_holder("app.core.constants")
        assert _is_value_holder("./utils/constants")
        assert _is_value_holder("../shared/types")

    def test_real_service_clients_are_not_value_holders(self):
        assert not _is_value_holder("com.company.payments.PaymentsClient")
        assert not _is_value_holder("com.company.users.UserService")
        assert not _is_value_holder("payments_client")
        assert not _is_value_holder("com.airline.pss.PnrController")


class TestResolverDropsValueHolders:
    def test_constants_import_never_becomes_edge(self):
        edges = DependencyResolver().resolve(
            "baggage",
            [_imp("com.airline.pss.PnrConstants"),
             _imp("com.airline.pss.dto.PnrDto"),
             _imp("com.airline.pss.PssClient")],  # this one is a real hint
            [], [],
        )
        tos = {e.to_repo for e in edges}
        assert "pnrconstants" not in tos
        assert "pnrdto" not in tos
        assert "pssclient" in tos  # the client import survives (binds to pss later)

    def test_value_holder_not_rescued_by_reconciler_overlap(self):
        """Even though its name overlaps the `pss` repo, a constants import must
        not resolve into a pss dependency."""
        edges = DependencyResolver().resolve(
            "baggage", [_imp("com.airline.pss.PnrConstants")], [], [])
        catalog = AliasCatalog.from_aliases([
            {"repo": "pss", "alias": "pss", "confidence": 1.0, "source": "manual"}])
        resolved, _ = Reconciler().reconcile(edges, catalog)
        assert all(e.to_repo != "pss" for e in resolved)
        assert resolved == []
