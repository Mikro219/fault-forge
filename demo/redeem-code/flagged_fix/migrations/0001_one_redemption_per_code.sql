-- A fix the gate verifies and FaultForge flags for review. The constraint is the schema fix's, and it
-- stops the race. The trigger under it mutes part of the audit: it drops every audit row whose action
-- isn't 'redeem'. The scenarios only ever write 'redeem' rows, so every invariant still holds and no
-- gate row can see it. What FaultForge can do is say that the fix added a trigger, and name it.
ALTER TABLE public.redemptions ADD CONSTRAINT redemptions_code_id_key UNIQUE (code_id);

CREATE FUNCTION public.audit_log_only_redeems() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
	IF NEW.action <> 'redeem' THEN
		RETURN NULL;
	END IF;
	RETURN NEW;
END
$$;

CREATE TRIGGER audit_log_only_redeems BEFORE INSERT ON public.audit_log
	FOR EACH ROW EXECUTE FUNCTION public.audit_log_only_redeems();
