"""GoPlus Solana token security: a second opinion on authorities and Token-2022 traps."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

from ..http import ApiError, get_json

# GoPlus field -> label shown in alerts.
TRAP_FIELDS = {
    "transfer_fee": "transfer fee",
    "transfer_hook": "transfer hook",
    "non_transferable": "non-transferable",
    "closable": "closable by authority",
    "balance_mutable_authority": "balances editable by authority",
    "default_account_state": "new accounts frozen by default",
}


@dataclass
class GoPlusFacts:
    mintable: bool
    freezable: bool
    traps: list[str] = field(default_factory=list)
    malicious_creator: bool = False


def _flag(value: Any) -> bool:
    """GoPlus encodes flags as "1", {"status": "1"}, or a non-empty list/fee object."""
    if isinstance(value, dict):
        if "status" in value:
            return str(value["status"]) == "1"
        rate = value.get("current_fee_rate") or value.get("fee_rate")
        if rate is not None:
            try:
                return float(rate) > 0
            except (TypeError, ValueError):
                return True
        return bool(value)
    if isinstance(value, list):
        return len(value) > 0
    return str(value) == "1"


def parse_security(data: dict[str, Any]) -> GoPlusFacts:
    state = data.get("default_account_state")
    if isinstance(state, str) and state.lower() in ("frozen", "2"):
        data = {**data, "default_account_state": "1"}
    return GoPlusFacts(
        mintable=_flag(data.get("mintable")),
        freezable=_flag(data.get("freezable")),
        traps=[label for key, label in TRAP_FIELDS.items() if _flag(data.get(key))],
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
