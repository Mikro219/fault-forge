// Redeems a code for a user. The bug FaultForge finds is here: the status is read, the kit is
// loaded, and only then is the code marked redeemed. Nothing holds the row in between, and the
// transaction around the writes doesn't check the status again, so two requests can both see
// `unused` and both redeem.
import { pool } from './db.ts';
import { loadKit } from './kits.ts';

export type RedeemResult =
	| { outcome: 'redeemed'; kit: string; items: string[] }
	| { outcome: 'already_redeemed' | 'revoked' | 'unknown_code' };

export async function redeem(code: string, user: string): Promise<RedeemResult> {
	const db = pool();
	const found = await db.query('SELECT id, status, kit FROM public.codes WHERE code = $1', [code]);
	const row = found.rows[0];
	if (row === undefined) return { outcome: 'unknown_code' };
	if (row.status === 'revoked') return { outcome: 'revoked' };
	if (row.status !== 'unused') return { outcome: 'already_redeemed' };

	const items = await loadKit(db, row.kit);

	const client = await db.connect();
	try {
		await client.query('BEGIN');
		await client.query("UPDATE public.codes SET status = 'redeemed' WHERE id = $1", [row.id]);
		await client.query('INSERT INTO public.redemptions (code_id, user_id) VALUES ($1, $2)', [row.id, user]);
		await client.query("INSERT INTO public.audit_log (action, code_id, user_id) VALUES ('redeem', $1, $2)", [
			row.id,
			user
		]);
		await client.query('COMMIT');
	} catch (error) {
		await client.query('ROLLBACK').catch(() => undefined);
		throw error;
	} finally {
		client.release();
	}
	return { outcome: 'redeemed', kit: row.kit, items };
}
