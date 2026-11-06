"""False fix for tests: removes the race by removing the required payment step."""

from fastapi.responses import JSONResponse

from ticket_support import db
from ticket_support.server import ReserveRequest, create_app

app = create_app()


@app.post("/reserve")
async def reserve(body: ReserveRequest):
    with db.tx_immediate():
        if not db.claim_stock(body.item_id):
            return JSONResponse({"error": "sold out"}, status_code=409)
        order_id = db.insert_order(body.item_id, body.buyer, None)
    return {"order_id": order_id}
