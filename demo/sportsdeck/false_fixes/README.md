# False fixes

`no_signature_check.ts` was written by `glm-5.3` in a real trial. It fixes the race and passes the main scenario, but it stops checking the token's signature. `forged-signature-rejected` exists to catch it.
