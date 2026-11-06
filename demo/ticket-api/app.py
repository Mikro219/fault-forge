"""Ticket reservation endpoint."""

from fastapi import Request
from fastapi.responses import JSONResponse

from ticket_support import db, payments
from ticket_support.server import ReserveRequest, create_app, request_key

app = create_app()


@app.post("/reserve")
async def reserve(body: ReserveRequest, request: Request):
    stock = db.get_stock(body.item_id)
    if stock is None:
        return JSONResponse({"error": "unknown item"}, status_code=404)
    if stock <= 0:
        return JSONResponse({"error": "sold out"}, status_code=409)

    key = request_key(request)
    try:
        auth = await payments.authorize(body.buyer, key=key)
    except payments.PaymentDeclined:
        return JSONResponse({"error": "payment declined"}, status_code=402)
    except payments.PaymentTimeout:
        await payments.void(key)
        return JSONResponse({"error": "payment timed out"}, status_code=503)

    db.set_stock(body.item_id, stock - 1)
    order_id = db.insert_order(body.item_id, body.buyer, auth.id)
    return {"order_id": order_id}
