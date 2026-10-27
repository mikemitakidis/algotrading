"""research.agents.roles — research-agent roles and a fail-closed permission gate (canonical B22, D06).

Roles (proposal r2 §7): Director, Scout, Hypothesist, Engineer, Skeptic,
Librarian, Reporter, plus a DETERMINISTIC Auditor that is code, not a
model. Each role gets an explicit allow-list of actions and the knowledge
namespaces it may write. Everything else is denied, and every decision,
allowed or denied, is appended to the hash-chained ledger.

Some actions are denied to EVERY role, whatever any configuration says:
- broker or order actions;
- network access;
- reading secrets;
- executing generated code;
- opening the holdout vault;
- merging or deploying;
- changing risk limits;
- scheduling paid agents.

The lane B authority model (a separately held signer) and the operator stay
outside this system.

This is an offline policy model for the mock workflow. A real deployment
would enforce the same rules at the OS and credential level. A Python
allow-list is not a sandbox (see ``research.boundary``).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Dict, FrozenSet, Optional

from research.integrity.ledger import Ledger

NEVER = frozenset({
    "broker.connect", "broker.order", "network.fetch", "secrets.read", "code.execute", "holdout.open",
    "repo.merge", "deploy", "risk.change", "agents.schedule_paid", "data.export_production",
})

ACTIONS = frozenset({
    # knowledge library
    "kb.read", "kb.write.source", "kb.write.hypothesis", "kb.write.rejection", "kb.write.lineage",
    "kb.write.review", "kb.write.report",
    # research objects
    "genome.propose", "genome.validate", "code.lint",
    # evaluation (deterministic auditor only)
    "eval.synthetic", "ledger.trial", "prereg.register",
    # orchestration
    "task.assign", "report.write",
}) | NEVER


@dataclass(frozen=True)
class Role:
    name: str
    purpose: str
    allowed: FrozenSet[str]
    deterministic: bool = False


ROLES: Dict[str, Role] = {r.name: r for r in (
    Role("director", "chooses research questions and allocates the bounded research budget",
         frozenset({"kb.read", "task.assign"})),
    Role("scout", "finds and cites sources from the offline fixture library (no network)",
         frozenset({"kb.read", "kb.write.source"})),
    Role("hypothesist", "turns sources into falsifiable hypotheses with mechanisms and kill criteria",
         frozenset({"kb.read", "kb.write.hypothesis", "genome.propose"})),
    Role("engineer", "turns a hypothesis into a genome specification (data, never code)",
         frozenset({"kb.read", "genome.propose", "genome.validate", "code.lint", "kb.write.lineage"})),
    Role("skeptic", "attacks proposals: duplicates, look-ahead citations, unfalsifiable claims, risky code text",
         frozenset({"kb.read", "kb.write.review", "code.lint", "genome.validate"})),
    Role("auditor", "deterministic evaluation on synthetic data with ledgered trials and report cards",
         frozenset({"kb.read", "eval.synthetic", "ledger.trial", "prereg.register", "genome.validate"}),
         deterministic=True),
    Role("librarian", "records outcomes, lineage and the rejection graveyard",
         frozenset({"kb.read", "kb.write.rejection", "kb.write.lineage", "kb.write.hypothesis"})),
    Role("reporter", "writes the human-readable summary",
         frozenset({"kb.read", "report.write", "kb.write.report"})),
)}


@dataclass(frozen=True)
class Decision:
    role: str
    action: str
    resource: str
    allowed: bool
    reason: str


class PermissionDenied(PermissionError):
    pass


class Gate:
    """Fail-closed authorisation with an audit trail."""

    def __init__(self, ledger: Ledger, roles: Optional[Dict[str, Role]] = None):
        self.ledger = ledger
        self.roles = dict(ROLES if roles is None else roles)
        for r in self.roles.values():
            if r.allowed & NEVER:
                raise ValueError("role %s may not be granted %s" % (r.name, sorted(r.allowed & NEVER)))

    def check(self, role: str, action: str, resource: str = "") -> Decision:
        if action in NEVER:
            d = Decision(role, action, resource, False, "never permitted to any research agent")
        elif role not in self.roles:
            d = Decision(role, action, resource, False, "unknown role")
        elif action not in ACTIONS:
            d = Decision(role, action, resource, False, "unknown action")
        elif action not in self.roles[role].allowed:
            d = Decision(role, action, resource, False, "not in the role's allow-list")
        else:
            d = Decision(role, action, resource, True, "allowed")
        self.ledger.append("agent_decision", asdict(d), ref_id=role)
        return d

    def require(self, role: str, action: str, resource: str = "") -> Decision:
        d = self.check(role, action, resource)
        if not d.allowed:
            raise PermissionDenied("%s may not %s (%s): %s" % (role, action, resource or "-", d.reason))
        return d


__all__ = ["NEVER", "ACTIONS", "Role", "ROLES", "Decision", "PermissionDenied", "Gate"]
