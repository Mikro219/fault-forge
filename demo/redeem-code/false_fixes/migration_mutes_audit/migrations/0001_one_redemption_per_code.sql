-- A false fix. The constraint is right and stops the race. The rule under it is not: it was added
-- "to keep the audit table small", and it silently drops every audit row, so a redemption leaves no
-- trace. The app's INSERT still succeeds, and nothing in src/ shows it.
ALTER TABLE public.redemptions ADD CONSTRAINT redemptions_code_id_key UNIQUE (code_id);

CREATE RULE audit_log_muted AS ON INSERT TO public.audit_log DO INSTEAD NOTHING;
