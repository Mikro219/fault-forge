# Session token business rules

- A refresh token can be redeemed exactly once, however many requests arrive at the same time.
- A valid, unexpired refresh token must be redeemable, and must return a new access token and a new refresh token.
- A token that was never issued, has expired, or was already redeemed gets a 401.
- A stored token whose signature doesn't verify gets a 401: being in the database isn't enough.
- Keep the route's request and response shapes.
