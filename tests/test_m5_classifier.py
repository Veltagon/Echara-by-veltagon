"""§4.2 contract failure classifier — deterministic, no model calls."""
from __future__ import annotations

from agents import classifier as C

_PREFIXES = {"core": "app.core", "inventory": "app.inventory", "orders": "app.orders"}
_SEAMS = {
    "inventory": [{"name": "reserve_stock", "signature": "def reserve_stock(db, sku, qty)"}],
    "core": [{"name": "get_db"}],
}


def _f(msg):  # minimal junit-failure dict
    return {"file": "app.orders.tests.test_x", "test": "t", "message": msg}


def test_interface_breach_routes_to_provider(tmp_path):
    # inventory DECLARED reserve_stock in SEAMS but its code doesn't export it.
    iface = {"inventory": {"release_stock", "Reservation"}, "orders": {"create_order"}}
    r = C.classify(_f("ImportError: cannot import name 'reserve_stock' from 'app.inventory.service'"),
                   owner="orders", seams=_SEAMS, iface_names=iface, prefixes=_PREFIXES)
    assert r["state"] == C.INTERFACE_BREACH
    assert r["target"] == "inventory"          # re-routed to the PROVIDER, not orders


def test_hallucination_stays_on_consumer():
    # orders imports a symbol no seam licenses → consumer invented it.
    iface = {"inventory": {"reserve_stock"}, "orders": {"create_order"}}
    r = C.classify(_f("ImportError: cannot import name 'ghost_fn' from 'app.inventory.service'"),
                   owner="orders", seams=_SEAMS, iface_names=iface, prefixes=_PREFIXES)
    assert r["state"] == C.UPSTREAM_HALLUCINATION
    assert r["target"] == "orders"             # fix the CONSUMER


def test_licensed_and_present_is_local():
    # reserve_stock is licensed AND on disk → the failure is consumer import wiring.
    iface = {"inventory": {"reserve_stock"}, "orders": {"create_order"}}
    r = C.classify(_f("ImportError: cannot import name 'reserve_stock' from 'app.inventory.service'"),
                   owner="orders", seams=_SEAMS, iface_names=iface, prefixes=_PREFIXES)
    assert r["state"] == C.LOCAL_BUG
    assert r["target"] == "orders"


def test_external_import_not_misrouted():
    # importing from a non-peer package → provider unresolved → owner, never a peer.
    r = C.classify(_f("ImportError: cannot import name 'FastAPI' from 'fastapi'"),
                   owner="orders", seams=_SEAMS, iface_names={}, prefixes=_PREFIXES)
    assert r["target"] == "orders"


def test_signature_mismatch_and_plain_failures_are_local():
    for msg in ("TypeError: create_order() got an unexpected keyword argument 'foo'",
                "AssertionError: assert 1 == 2",
                "ModuleNotFoundError: No module named 'app.inventory'"):
        r = C.classify(_f(msg), owner="orders", seams=_SEAMS, iface_names={}, prefixes=_PREFIXES)
        assert r["state"] == C.LOCAL_BUG and r["target"] == "orders", msg


def test_interface_names_reads_disk(tmp_path):
    (tmp_path / "interfaces").mkdir()
    (tmp_path / "interfaces" / "core.md").write_text(
        "## core\ndef get_db() -> Session\nclass Base", encoding="utf-8")
    names = C.interface_names(tmp_path, ["core", "missing"])
    assert "get_db" in names["core"] and "Base" in names["core"]
    assert names["missing"] == set()          # absent module → empty, not a crash
