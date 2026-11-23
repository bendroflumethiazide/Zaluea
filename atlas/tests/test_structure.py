"""Structural guarantees, checked by reading the source code itself."""
import ast
from pathlib import Path

PKG = Path(__file__).resolve().parents[1] / "atlas"


def imports_of(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            out |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            out.add(("." * n.level) + (n.module or ""))
    return out


def test_shadow_risk_manager_shares_no_code_with_atlas():
    allowed_roots = {"__future__", "base64", "json", "sqlite3", "time", "dataclasses", "datetime", "decimal",
                     "pathlib", "zoneinfo", "os", "cryptography"}
    for f in (PKG / "shadowrisk").glob("*.py"):
        for mod in imports_of(f):
            assert not mod.startswith("."), f"{f.name} imports ATLAS code: {mod}"
            assert not mod.startswith("atlas"), f"{f.name} imports ATLAS code: {mod}"
            assert mod.split(".")[0] in allowed_roots, f"{f.name} imports {mod}"


def test_only_the_gateway_places_orders():
    callers = set()
    for f in PKG.rglob("*.py"):
        tree = ast.parse(f.read_text())
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "place_order":
                callers.add(f.relative_to(PKG).as_posix())
    # The single exception is the owner-run connection check: its demo order test
    # places one unfillable 1-contract order and refuses anything but the DEMO
    # exchange (see tests/test_check_connection.py::test_demo_order_test_refuses_production).
    assert callers == {"gateway.py", "check_connection.py"}, callers
    src = (PKG / "check_connection.py").read_text()
    fn = src[src.index("def demo_order_test"):src.index("def check_alpaca")]
    assert fn.index('if a.kalshi_env != "demo"') < fn.index("place_order"), "demo guard must come first"


def test_no_floats_in_money_ledger_or_limit_code():
    for name in ("money.py", "ledger.py", "limits.py"):
        tree = ast.parse((PKG / name).read_text())
        for n in ast.walk(tree):
            assert not (isinstance(n, ast.Constant) and isinstance(n.value, float)), f"float literal in {name}"
            assert not (isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "float"), \
                f"float() call in {name}"


def test_primary_risk_compares_money_not_floats():
    """In the primary risk manager the only float arithmetic allowed is quote age (time)."""
    tree = ast.parse((PKG / "risk" / "primary.py").read_text())
    floats = [n for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, float)]
    assert all(n.value == 30.0 for n in floats), "only the reconciliation age constant may be a float"
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "float"]
    assert not calls
