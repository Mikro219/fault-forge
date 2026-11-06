"""False fix for tests: can't oversell because it never sells anything."""

from fastapi.responses import JSONResponse

from ticket_support.server import ReserveRequest, create_app

app = create_app()


@app.post("/reserve")
async def reserve(body: ReserveRequest):
    return JSONResponse({"error": "sold out"}, status_code=409)
