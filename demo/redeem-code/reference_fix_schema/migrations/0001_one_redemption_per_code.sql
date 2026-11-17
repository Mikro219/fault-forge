-- A code has at most one redemption, enforced by the schema: a second insert for the same code
-- fails with a unique violation (23505), whatever the app read beforehand.
ALTER TABLE public.redemptions ADD CONSTRAINT redemptions_code_id_key UNIQUE (code_id);
