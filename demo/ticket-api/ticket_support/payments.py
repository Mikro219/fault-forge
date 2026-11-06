"""In-process payment provider stub with a fixed delay. It makes no external calls.

Buyer name prefixes trigger deterministic failures for tests: decline-, timeout-,
crash- (attach raises) and voidfail- (void can't complete).
"""

import asyncio
import os
from dataclasses import dataclass

from ticket_support import db


class PaymentDeclined(Exception):
    pass


class PaymentTimeout(Exception):
    pass


class AttachFailed(Exception):
    pass


@dataclass(frozen=True)
class Authorization:
    id: str


async def authorize(buyer: str, key: str) -> Authorization:
    """Places a hold on the buyer's card. A timed-out hold may still exist, so void it by key."""
    await asyncio.sleep(int(os.environ.get("FF_AUTH_DELAY_MS", "50")) / 1000)
    if buyer.startswith("decline-"):
        raise PaymentDeclined(buyer)
    db.connection().execute(
        "INSERT OR IGNORE INTO payments (id, buyer, status) VALUES (?, ?, 'authorized')",
        (key, buyer),
    )
    if buyer.startswith("timeout-"):
        raise PaymentTimeout(buyer)
    return Authorization(id=key)


def attach(payment_id: str, order_id: int) -> None:
    """Confirms a hold belongs to an order. Runs inside the caller's transaction."""
    row = db.connection().execute(
        "SELECT buyer FROM payments WHERE id = ?", (payment_id,)
    ).fetchone()
    if row is None or row[0].startswith("crash-"):
        raise AttachFailed(f"can't attach {payment_id} to order {order_id}")


async def void(payment_id: str) -> bool:
    """Releases a hold. Idempotent. A failed void leaves void_pending, which stays visible."""
    conn = db.connection()
    row = conn.execute("SELECT buyer FROM payments WHERE id = ?", (payment_id,)).fetchone()
    if row is None:
        return True
    status = "void_pending" if row[0].startswith("voidfail-") else "voided"
    conn.execute("UPDATE payments SET status = ? WHERE id = ?", (status, payment_id))
    return status == "voided"
