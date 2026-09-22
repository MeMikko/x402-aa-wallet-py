"""A requests.Session that auto-pays x402 challenges via a plain EOA —
works against ANY x402 "exact"-scheme merchant."""

from __future__ import annotations

from typing import Any, Union

import requests
from eth_account.signers.local import LocalAccount

from .wallet import SpendWallet

#: Base mainnet, CAIP-2 form — the default network, and the only one
#: registered unless `network` overrides it.
NETWORK = "eip155:8453"

#: USDC's own decimals on every chain x402 currently settles on (Base
#: included) — used to convert a human max_amount_usd into the atomic
#: units payment requirements are expressed in.
_USDC_DECIMALS = 6

#: Circle's own native USDC contract addresses, keyed by CAIP-2 network —
#: every one is 6 decimals (Circle mandates this across all its official
#: deployments, unlike third-party bridged/wrapped variants elsewhere).
#: A PaymentRequirements.asset is just an address; nothing about the x402
#: protocol guarantees it points at one of these. _max_amount_usd_policy
#: only applies the _USDC_DECIMALS-based cap to a requirement whose asset
#: matches an entry here — anything else is excluded rather than
#: evaluated with a guessed decimal count. Guessing UNDER the real decimal
#: count (e.g. treating an 18-decimal asset's raw amount as 6-decimal)
#: makes a genuinely large charge look small enough to pass the cap — a
#: fail-open worse than refusing to pay an asset this policy can't verify.
_KNOWN_USDC_ADDRESSES = {
    "eip155:8453": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # Base mainnet
    "eip155:84532": "0x036cbd53842c5426634e7929541ec2318f3dcf7e",  # Base Sepolia
}


def _is_verified_usdc(r: Any) -> bool:
    known_usdc = _KNOWN_USDC_ADDRESSES.get(r.network)
    if known_usdc is None or r.asset.lower() != known_usdc:
        return False
    # A negative amount is malformed. uint256 encoding would reject it
    # downstream anyway, but a spend policy should never call it payable.
    return int(r.get_amount()) >= 0


def _describe(requirements: list[Any]) -> str:
    return ", ".join(
        f"{r.get_amount()} atomic units of {r.asset} on {r.network}" for r in requirements
    )


def _payment_policy(max_amount_usd: float | None, max_total_usd: float | None):
    """Refuse (raise) rather than pay any requirement that fails asset
    verification, the per-call cap, or the cumulative session budget — a
    real x402Client policy (see register_policy), not a client-side amount
    check bolted on after signing. Mirrors the TypeScript implementation's
    behavior exactly."""
    per_call_cap_atomic = (
        round(max_amount_usd * 10**_USDC_DECIMALS) if max_amount_usd is not None else None
    )
    total_cap_atomic = (
        round(max_total_usd * 10**_USDC_DECIMALS) if max_total_usd is not None else None
    )
    # Cumulative authorization ledger for this session. Counted at approval
    # time (see x402_session's max_total_usd docs for why), so it only ever
    # over-counts — never under.
    authorized_atomic = 0

    def policy(_version: int, requirements: list[Any]) -> list[Any]:
        nonlocal authorized_atomic
        if not requirements:
            return requirements

        verified = [r for r in requirements if _is_verified_usdc(r)]
        if not verified:
            raise ValueError(
                f"x402_session: none of the payment options ({_describe(requirements)}) is on "
                "a recognized Circle USDC deployment (unrecognized asset, decimals not "
                "verified) — refusing to sign. An unverified asset cannot be measured against "
                "a USD cap, and an EIP-3009 signature is valid for whatever token it names. "
                "Pass allow_unknown_assets=True (with no max_amount_usd/max_total_usd) only "
                "if this wallet holds nothing you are not willing to lose."
            )

        under_per_call = (
            verified
            if per_call_cap_atomic is None
            else [r for r in verified if int(r.get_amount()) <= per_call_cap_atomic]
        )
        if not under_per_call:
            raise ValueError(
                f"x402_session: every payment option ({_describe(verified)}) exceeds the "
                f"configured max_amount_usd cap of ${max_amount_usd} — refusing to pay. "
                "Raise max_amount_usd if this charge is expected, or investigate why the "
                "server is asking for more than usual."
            )

        if total_cap_atomic is None:
            return under_per_call

        within_budget = [
            r
            for r in under_per_call
            if authorized_atomic + int(r.get_amount()) <= total_cap_atomic
        ]
        if not within_budget:
            authorized_usd = authorized_atomic / 10**_USDC_DECIMALS
            raise ValueError(
                f"x402_session: paying any offered option ({_describe(under_per_call)}) would "
                f"push this session past its max_total_usd budget of ${max_total_usd} "
                f"(already authorized ${authorized_usd}) — refusing to pay. Build a new "
                "x402_session to start a fresh budget if this spending is intended."
            )
        # The client settles ONE of the requirements this policy returns. To
        # keep the ledger honest, return exactly one — the cheapest — and
        # count it as authorized now.
        cheapest = min(within_budget, key=lambda r: int(r.get_amount()))
        authorized_atomic += int(cheapest.get_amount())
        return [cheapest]

    return policy


def x402_session(
    wallet: Union[SpendWallet, LocalAccount, str],
    *,
    max_amount_usd: float | None = None,
    max_total_usd: float | None = None,
    allow_unknown_assets: bool = False,
    network: str = NETWORK,
) -> requests.Session:
    """Build a `requests.Session` that transparently pays any HTTP 402
    x402 challenge it hits, signing with `wallet`.

    Args:
        wallet: a `SpendWallet` (from `create_spend_wallet`), a raw
            `eth_account` `LocalAccount`, or a private key hex string.
            Every payment this makes is real USDC on Base mainnet — only
            fund this wallet with what you're willing to spend, and never
            reuse an EOA that also holds other funds you care about.
        max_amount_usd: refuse to pay any single 402 challenge above this
            USD amount. Without this, the session pays whatever the
            server's 402 response asks for — a misbehaving or compromised
            merchant returning far more than expected gets paid in full,
            silently. Recommended for any caller giving an agent
            autonomous spending. Note this is PER CHALLENGE: a merchant
            charging exactly at the cap on every request still drains
            cap × N over N requests — pair it with ``max_total_usd`` to
            bound the whole session.
        max_total_usd: cumulative budget for everything this session
            authorizes, in USD. Once authorized payments reach this
            budget, further 402 challenges raise instead of paying — the
            backstop ``max_amount_usd`` alone cannot provide against
            drain-by-repetition. Accounting is at AUTHORIZATION time,
            deliberately conservative: the amount is counted the moment
            the policy approves a requirement for signing, not when
            settlement is confirmed. A payment that later fails still
            consumes budget (the signature left the process — from a
            spend-control standpoint the money must be presumed gone).
            The ledger lives inside this session; build a new
            ``x402_session`` to start a fresh budget.
        allow_unknown_assets: by default every payment requirement must
            name a known Circle USDC deployment before this library will
            sign anything — EVEN when no cap is set. An EIP-3009
            authorization is valid for whatever token contract it names,
            so without this check a merchant could induce a signature
            moving ANY EIP-3009 token the EOA happens to hold. Pass True
            to sign for unrecognized assets anyway — only sensible when
            the wallet holds nothing but funds you are willing to lose,
            and never honored while ``max_amount_usd``/``max_total_usd``
            is set (an asset with unverified decimals cannot be measured
            against a USD cap).
        network: override the network this wallet pays on — CAIP-2 form.
            Defaults to `NETWORK` (Base mainnet). Only Base/EVM
            "exact"-scheme payments are supported by this library today
            regardless of the value passed here.
    """
    # Imported lazily so importing this package doesn't require x402's EVM
    # extras unless a session is actually built.
    from x402 import x402ClientSync
    from x402.http.clients import wrapRequestsWithPayment
    from x402.mechanisms.evm.exact import ExactEvmScheme

    if isinstance(wallet, SpendWallet):
        account = wallet.account
    elif isinstance(wallet, str):
        from .wallet import spend_wallet_from_private_key

        account = spend_wallet_from_private_key(wallet).account
    else:
        account = wallet

    session = requests.Session()
    x402_client = x402ClientSync()
    if hasattr(x402_client, "set_spend_controls"):
        x402_client.set_spend_controls(False)
    x402_client.register(network, ExactEvmScheme(account))
    # The asset-verification policy is ALWAYS on unless the caller both
    # sets no cap and explicitly opts into unknown assets. With a cap set,
    # allow_unknown_assets is deliberately ignored: an asset with
    # unverified decimals cannot be measured against a USD cap.
    has_cap = max_amount_usd is not None or max_total_usd is not None
    if has_cap or not allow_unknown_assets:
        x402_client.register_policy(_payment_policy(max_amount_usd, max_total_usd))
    wrapRequestsWithPayment(session, x402_client)
    return session
