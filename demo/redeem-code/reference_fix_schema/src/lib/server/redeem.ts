// The schema fix: migrations/0001_one_redemption_per_code.sql lets a code have one redemption, so the
// database refuses the loser of a race. Its whole transaction rolls back, the status change and the
// audit row included, and the unique violation is answered as "already redeemed" (409).
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
		const failed = error as { code?: string; constraint?: string };
		if (failed.code === '23505' && failed.constraint === 'redemptions_code_id_key') return { outcome: 'already_redeemed' };
		throw error;
	} finally {
		client.release();
	}
	return { outcome: 'redeemed', kit: row.kit, items };
}
