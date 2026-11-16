// False fix: the claim is atomic, but it only excludes redeemed codes, so a revoked code redeems.
// The race passes; the revoked-code-refused regression scenario catches it.
import { pool } from './db.ts';
import { loadKit } from './kits.ts';

export type RedeemResult =
	| { outcome: 'redeemed'; kit: string; items: string[] }
	| { outcome: 'already_redeemed' | 'revoked' | 'unknown_code' };

export async function redeem(code: string, user: string): Promise<RedeemResult> {
	const client = await pool().connect();
	try {
		await client.query('BEGIN');
		const claimed = await client.query(
			"UPDATE public.codes SET status = 'redeemed' WHERE code = $1 AND status <> 'redeemed' RETURNING id, kit",
			[code]
		);
		const row = claimed.rows[0];
		if (row === undefined) {
			await client.query('ROLLBACK');
			const found = await client.query('SELECT 1 FROM public.codes WHERE code = $1', [code]);
			return { outcome: found.rows.length === 0 ? 'unknown_code' : 'already_redeemed' };
		}
		const items = await loadKit(client, row.kit);
		await client.query('INSERT INTO public.redemptions (code_id, user_id) VALUES ($1, $2)', [row.id, user]);
		await client.query("INSERT INTO public.audit_log (action, code_id, user_id) VALUES ('redeem', $1, $2)", [
			row.id,
			user
		]);
		await client.query('COMMIT');
		return { outcome: 'redeemed', kit: row.kit, items };
	} catch (error) {
		await client.query('ROLLBACK').catch(() => undefined);
		throw error;
	} finally {
		client.release();
	}
}
