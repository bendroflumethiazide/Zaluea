"""Where ATLAS keeps its durable state, and the one-time setup."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DataPaths:
    root: Path

    @property
    def audit(self): return self.root / "audit.db"
    @property
    def ledger(self): return self.root / "ledger.db"
    @property
    def control(self): return self.root / "control.db"
    @property
    def locks(self): return self.root / "locks.db"
    @property
    def health(self): return self.root / "health.db"
    @property
    def shadow_journal(self): return self.root / "shadowrisk.db"
    @property
    def keys(self): return self.root / "keys"
    @property
    def shadow_private_key(self): return self.keys / "shadow_signing.key"
    @property
    def shadow_public_key(self): return self.keys / "shadow_signing.pub"
    @property
    def gateway_lock(self): return self.root / "gateway.lock"
    @property
    def watchdog_lock(self): return self.root / "watchdog.lock"


def initialize(root: str | Path) -> DataPaths:
    """Create a brand-new ATLAS data folder. Refuses to touch an existing one,
    so a real ledger can never be silently replaced by an empty one."""
    from .audit import AuditLog
    from .control import ControlStore, LockStore
    from .health import HealthBoard
    from .ledger import Ledger
    from .limits import LimitStore
    from .shadowrisk.checker import generate_keypair

    p = DataPaths(Path(root))
    if p.ledger.exists() or p.audit.exists():
        raise FileExistsError(f"{p.root} already holds ATLAS data; refusing to initialize over it")
    p.root.mkdir(parents=True, exist_ok=True)
    audit = AuditLog(p.audit, create=True)
    Ledger(p.ledger, audit, create=True)
    ControlStore(p.control, audit, create=True)
    LimitStore(p.control, audit, create=True)
    LockStore(p.locks, audit, create=True)
    HealthBoard(p.health, create=True)
    p.keys.mkdir(exist_ok=True)
    priv, pub = generate_keypair()
    p.shadow_private_key.write_bytes(priv)
    p.shadow_public_key.write_bytes(pub)
    audit.append("setup", decision="initialized", reason="New ATLAS data folder created")
    return p
