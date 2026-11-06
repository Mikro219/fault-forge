"""Reference fix, for rehearsal and tests only.

Authorize, then atomically claim stock and write the order, then void on any failure.
Reserving before authorizing is the production alternative.
"""

from fastapi import Request
from fastapi.responses import JSONResponse

from ticket_support import db, payments
from ticket_support.server import ReserveRequest, create_app, request_key

app = create_app()


@app.post("/reserve")
async def reserve(body: ReserveRequest, request: Request):
    key = request_key(request)
    try:
        auth = await payments.authorize(body.buyer, key=key)
    except payments.PaymentDeclined:
        return JSONResponse({"error": "payment declined"}, status_code=402)
    except payments.PaymentTimeout:
        await payments.void(key)
        return JSONResponse({"error": "payment timed out"}, status_code=503)

    order_id = None
    try:
        with db.tx_immediate():
            if db.claim_stock(body.item_id):
                order_id = db.insert_order(body.item_id, body.buyer, auth.id)
                payments.attach(auth.id, order_id)
    except Exception:
        await payments.void(auth.id)
        raise

    if order_id is None:
        await payments.void(auth.id)
        return JSONResponse({"error": "sold out"}, status_code=409)
    return {"order_id": order_id}
