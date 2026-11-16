# Redeem-code business rules

- An unused code can be redeemed exactly once, however many requests arrive at the same time.
- The one winner gets 200. Every other request for that code gets 409.
- A code that was already redeemed gets 409, a revoked code gets 410 and an unknown code gets 404.
- Every redemption writes exactly one row to `redemptions` and one `redeem` row to `audit_log`.
- Redeeming one code never changes another code.
- Keep the route's request and response shapes.
