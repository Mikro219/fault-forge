"""False fix for tests: stops overselling, but losing buyers keep their payment holds."""

from fastapi import Request
from fastapi.responses import JSONResponse

from ticket_support import db, payments
from ticket_support.server import ReserveRequest, create_app, request_key

app = create_app()


@app.post("/reserve")
async def reserve(body: ReserveRequest, request: Request):
    auth = await payments.authorize(body.buyer, key=request_key(request))
    with db.tx_immediate():
        if not db.claim_stock(body.item_id):
            return JSONResponse({"error": "sold out"}, status_code=409)
        order_id = db.insert_order(body.item_id, body.buyer, auth.id)
    return {"order_id": order_id}
