"""Local, non-custodial EOA generation for x402 payments.

Why this exists: x402's "exact" EVM scheme settles payments via EIP-3009
transferWithAuthorization, which recovers the payer's address from an
ECDSA (secp256k1) signature. An ERC-4337 smart-contract wallet's owner key
is often a P256/WebAuthn passkey (a different curve entirely — not
ecrecover-compatible) or, even when it is a secp256k1 key, the signature
recovers to the OWNER's address, not the smart-contract wallet's own
address, which the facilitator's from-address check rejects. Full
ERC-1271/ERC-6492 smart-wallet support is an open, unshipped feature in
the ecosystem's facilitators (see
https://github.com/coinbase/x402/issues/639) — so today, a plain EOA is
the one signer type that reliably works everywhere.

The fix here is NOT to route your AA wallet's signature through x402
directly — it's to give your agent a small, DEDICATED EOA it funds itself
(from its own smart wallet, via its own existing infrastructure) purely
for x402 spending, separate from whatever wallet it uses for everything
else.

Non-custodial, by construction: this module never transmits, logs, or
persists a private key anywhere. Key generation happens entirely inside
your own process via eth_account; the private key is returned to YOU and
exists only in your process's memory unless you choose to store it. This
package has no visibility into it, ever.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from eth_account import Account
from eth_account.signers.local import LocalAccount


@dataclass(frozen=True)
class SpendWallet:
    """A freshly generated, dedicated EOA for x402 payments.

    Store `private_key` yourself (env var, secret manager, wherever you
    normally keep secrets) — this library keeps no copy of it once this
    function returns.
    """

    address: str
    #: Excluded from repr() (repr=False) — dataclasses generate a __repr__
    #: that prints every field by default, and a frozen dataclass's
    #: private_key printed via print()/logging/an unhandled exception's
    #: traceback is exactly the kind of accidental leak this library
    #: exists to avoid. Direct attribute access (wallet.private_key) is
    #: completely unaffected — this only changes what repr() shows.
    #:
    #: KNOWN LIMIT — repr() is the ONLY vector this hides. Anything that
    #: walks __dict__ still sees the key: vars(wallet),
    #: dataclasses.asdict(wallet), json.dumps(asdict(...)), and structured
    #: loggers / error reporters that serialize object attributes (some
    #: structlog/Sentry processors do). eth_account's own
    #: wallet.account.key also always holds the raw key bytes. Python
    #: offers no dataclass equivalent of the TypeScript package's
    #: non-enumerable property, so do not feed this object to a
    #: dict-serializing sink — pass wallet.address around instead.
    private_key: str = field(repr=False)
    account: LocalAccount
    """Ready to pass straight into :func:`x402_aa_wallet.x402_session`."""


def create_spend_wallet() -> SpendWallet:
    """Generate a new secp256k1 EOA locally — entirely in this process,
    never transmitted anywhere. Fund the returned `address` with USDC on
    Base from your agent's own smart wallet (using its own existing
    send/transfer capability — this library never moves funds itself),
    then use `private_key` (or `account`) with `x402_session`.
    """
    account = Account.create()
    raw_hex = account.key.hex()
    private_key = raw_hex if raw_hex.startswith("0x") else f"0x{raw_hex}"
    return SpendWallet(address=account.address, private_key=private_key, account=account)


def spend_wallet_from_private_key(private_key: str) -> SpendWallet:
    """Rehydrate a `SpendWallet` from a private key you already generated
    and stored yourself (e.g. loaded from an env var on agent restart) —
    the counterpart to `create_spend_wallet` for a wallet you've already
    funded and don't want to regenerate.
    """
    account = Account.from_key(private_key)
    raw_hex = account.key.hex()
    normalized = raw_hex if raw_hex.startswith("0x") else f"0x{raw_hex}"
    return SpendWallet(address=account.address, private_key=normalized, account=account)
