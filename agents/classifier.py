"""§4.2 contract-based failure classifier — deterministic, zero model tokens.

Given one pytest/junit failure, decide who is at fault against the immutable
SEAMS.json contract, so a fix routes to the real culprit module rather than
whoever happens to own the failing test:

  LOCAL_BUG              — a bug in the owning module → re-invoke it (as today)
  INTERFACE_BREACH       — a PROVIDER declared a seam it did NOT deliver → fix the
                           provider (the consumers were right to import it)
  UPSTREAM_HALLUCINATION — a CONSUMER imported a symbol NO seam licenses → fix the
                           consumer (the symbol never existed)

Fault is assigned from the contract, never from execution order, so
consumer/provider blame cannot oscillate. Conservative on TARGET: the only case
that re-routes to a *different* module than the test owner is a high-confidence
INTERFACE_BREACH (declared in SEAMS, absent on disk); everything else stays on
the owner with an informative reason. Signature-mismatch and module-not-found
resolve to LOCAL (precise call-site resolution needs the file DAG, §2.1 —
deferred), so the classifier never mis-routes those away from the owner.
"""
from __future__ import annotations

import re
from pathlib import Path

LOCAL_BUG = "LOCAL_BUG"
INTERFACE_BREACH = "INTERFACE_BREACH"
UPSTREAM_HALLUCINATION = "UPSTREAM_HALLUCINATION"

# Only the two forms that name a symbol AND its source module are cross-module
# evidence. "No module named", NameError, etc. are packaging/local issues.
_RE_IMPORT = re.compile(r"cannot import name\s+'(?P<sym>\w+)'\s+from\s+'(?P<src>[\w.]+)'")
_RE_ATTR = re.compile(
    r"AttributeError:\s+(?:module\s+)?'?(?P<obj>[\w.]+)'?\s+(?:object\s+)?"
    r"has no attribute\s+'(?P<attr>\w+)'")
_RE_SIG = re.compile(r"TypeError:\s+[\w.]+\(\)\s+"
                     r"(?:got an unexpected keyword argument|takes|missing\s+\d+\s+required)")


def interface_names(build_dir: Path, modules: list[str]) -> dict[str, set[str]]:
    """{module: set of identifier tokens in its on-disk interface index} — 'what
    the module actually exports right now', the disk truth a seam is checked
    against (same token scan as interfaces.check_seams)."""
    out: dict[str, set[str]] = {}
    for m in modules:
        p = Path(build_dir) / "interfaces" / f"{m}.md"
        text = p.read_text(encoding="utf-8", errors="replace") if p.is_file() else ""
        out[m] = set(re.findall(r"\b([A-Za-z_][\w]*)\b", text))
    return out


def _seam_names(seams: dict, module: str | None) -> set[str]:
    if not module:
        return set()
    return {(e["name"] if isinstance(e, dict) else str(e))
            for e in (seams.get(module, []) or [])}


def _module_of(dotted: str | None, prefixes: dict[str, str]) -> str | None:
    """Longest import-prefix match: 'app.inventory.service' -> 'inventory'.
    None if the source is external (fastapi, sqlalchemy) or unresolvable."""
    if not dotted:
        return None
    best, blen = None, -1
    for name, pref in prefixes.items():
        if pref and (dotted == pref or dotted.startswith(pref + ".")) and len(pref) > blen:
            best, blen = name, len(pref)
    return best


def _extract(message: str) -> tuple[str | None, str | None, bool]:
    """(missing_symbol, provider_dotted, is_signature_mismatch) from the message."""
    m = _RE_IMPORT.search(message)
    if m:
        return m.group("sym"), m.group("src"), False
    m = _RE_ATTR.search(message)
    if m:
        return m.group("attr"), m.group("obj"), False
    if _RE_SIG.search(message):
        return None, None, True
    return None, None, False


def _route(state: str, target: str | None, symbol: str | None, reason: str) -> dict:
    return {"state": state, "target": target, "symbol": symbol, "reason": reason}


def classify(failure: dict, owner: str | None, seams: dict,
             iface_names: dict[str, set[str]], prefixes: dict[str, str]) -> dict:
    """Classify one failure. `owner` = module owning the failing test (or None).
    Returns {state, target, symbol, reason}; `target` is the module to fix."""
    sym, provider_dotted, is_sig = _extract(failure.get("message", ""))
    if sym is None:
        reason = ("signature mismatch — fix at the call/owner site" if is_sig
                  else "no cross-module symbol — local bug")
        return _route(LOCAL_BUG, owner, None, reason)

    provider = _module_of(provider_dotted, prefixes)
    licensed = _seam_names(seams, provider)
    if provider is None or sym not in licensed:
        # No seam of a resolved peer licenses this symbol → the consumer invented
        # it. Fix the consumer (owner); target stays the owner, never re-routed.
        src = provider_dotted or "?"
        return _route(UPSTREAM_HALLUCINATION, owner, sym,
                      f"'{sym}' from '{src}' is licensed by no seam — the consumer "
                      f"must not import it (remove the call or route via a real seam)")
    if sym not in iface_names.get(provider, set()):
        # Declared in SEAMS.json for the provider, absent from its code → the
        # PROVIDER broke contract. This is the one case we re-route (to provider).
        return _route(INTERFACE_BREACH, provider, sym,
                      f"'{sym}' is declared in SEAMS.json for module '{provider}' but "
                      f"is missing from its code — the provider must implement it")
    # Licensed AND present on disk, yet unresolved at runtime → the consumer's
    # own import wiring (wrong path style / missing __init__): local.
    return _route(LOCAL_BUG, owner, sym,
                  f"'{sym}' exists on '{provider}' — fix the import path in the consumer")
