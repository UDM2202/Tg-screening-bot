"""GoPlus Solana token security: a second opinion on authorities and Token-2022 traps."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

from ..http import ApiError, get_json

# GoPlus status-style fields -> label shown in alerts.
TRAP_FIELDS = {
    "transfer_hook": "transfer hook",
    "non_transferable": "non-transferable",
    "closable": "closable by authority",
    "balance_mutable_authority": "balances editable by authority",
}

# SPL account states: 0 = uninitialized, 1 = initialized (normal), 2 = frozen.
FROZEN_ACCOUNT_STATES = {"2", "frozen"}


@dataclass
class GoPlusFacts:
    mintable: bool
    freezable: bool
    traps: list[str] = field(default_factory=list)
    malicious_creator: bool = False


def _flag(value: Any) -> bool:
    """GoPlus encodes flags as "1", {"status": "1"}, or a non-empty list."""
    if isinstance(value, dict):
        return str(value.get("status")) == "1"
    if isinstance(value, list):
        return len(value) > 0
    return str(value) == "1"


def _has_fee(value: Any) -> bool:
    """True if any fee rate in GoPlus's (possibly nested) transfer_fee object is above zero."""
    if isinstance(value, dict):
        for key, inner in value.items():
            if "fee_rate" in key and not isinstance(inner, (dict, list)):
                try:
                    if float(inner) > 0:
                        return True
                except (TypeError, ValueError):
                    pass
            elif _has_fee(inner):
                return True
    elif isinstance(value, list):
        return any(_has_fee(v) for v in value)
    return False


def _frozen_by_default(value: Any) -> bool:
    if isinstance(value, dict):
        value = value.get("status", value.get("state"))
    return str(value).strip().lower() in FROZEN_ACCOUNT_STATES


def parse_security(data: dict[str, Any]) -> GoPlusFacts:
    traps = [label for key, label in TRAP_FIELDS.items() if _flag(data.get(key))]
    if _has_fee(data.get("transfer_fee")):
        traps.insert(0, "transfer fee")
    if _frozen_by_default(data.get("default_account_state")):
        traps.append("new accounts frozen by default")
    return GoPlusFacts(
        mintable=_flag(data.get("mintable")),
        freezable=_flag(data.get("freezable")),
        traps=traps,
        malicious_creator=any(
            str(c.get("malicious_address")) == "1" for c in data.get("creators") or []
        ),
    )


class GoPlus:
    def __init__(self, client: httpx.AsyncClient, base_url: str):
        self.client = client
        self.base = base_url.rstrip("/")

    async def security(self, mint: str) -> GoPlusFacts:
        body = await get_json(
            self.client,
            f"{self.base}/api/v1/solana/token_security",
            params={"contract_addresses": mint},
        )
        result = (body or {}).get("result") or {}
        data = result.get(mint) or next(iter(result.values()), None)
        if not data:
            raise ApiError(f"GoPlus has no data for {mint}: {body.get('message')}")
        return parse_security(data)
