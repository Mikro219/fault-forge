-- Applied once by tools/migrate.ts, as the owner, before the app starts. The app logs in as
-- redeem_app and owns nothing: the grants at the end are everything it may do.
CREATE TABLE IF NOT EXISTS public.codes (
	id serial PRIMARY KEY,
	code text NOT NULL UNIQUE,
	kit text NOT NULL DEFAULT 'starter',
	status text NOT NULL DEFAULT 'unused' CHECK (status IN ('unused', 'redeemed', 'revoked'))
);

-- No unique index on code_id: nothing in the schema stops a code being redeemed twice.
CREATE TABLE IF NOT EXISTS public.redemptions (
	id serial PRIMARY KEY,
	code_id integer NOT NULL REFERENCES public.codes (id),
	user_id text NOT NULL,
	at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.audit_log (
	id serial PRIMARY KEY,
	action text NOT NULL,
	code_id integer NOT NULL,
	user_id text NOT NULL,
	at timestamptz NOT NULL DEFAULT now()
);

-- What a kit unlocks. Seeded here once; a reset never touches it.
CREATE TABLE IF NOT EXISTS public.kit_items (
	id serial PRIMARY KEY,
	kit text NOT NULL,
	position integer NOT NULL,
	name text NOT NULL,
	UNIQUE (kit, position)
);

INSERT INTO public.kit_items (kit, position, name)
SELECT 'starter', n, 'item-' || n FROM generate_series(1, 40) AS n
ON CONFLICT (kit, position) DO NOTHING;

-- The app role, by name. It reads and claims codes, adds redemptions and audit rows, and reads kits.
-- It can't delete or rewrite a redemption or an audit row, and it can't create or alter anything.
GRANT USAGE ON SCHEMA public TO redeem_app;
GRANT SELECT, UPDATE ON public.codes TO redeem_app;
GRANT SELECT, INSERT ON public.redemptions, public.audit_log TO redeem_app;
GRANT USAGE ON SEQUENCE public.redemptions_id_seq, public.audit_log_id_seq TO redeem_app;
GRANT SELECT ON public.kit_items TO redeem_app;
