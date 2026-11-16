// False fix: the first redemption marks every unused code redeemed, so the race has one winner
// and every other code is destroyed. Fails the rule that other codes are left alone.
import { pool } from './db.ts';
import { loadKit } from './kits.ts';

export type RedeemResult =
	| { outcome: 'redeemed'; kit: string; items: string[] }
	| { outcome: 'already_redeemed' | 'revoked' | 'unknown_code' };

export async function redeem(code: string, user: string): Promise<RedeemResult> {
	const client = await pool().connect();
	try {
		await client.query('BEGIN');
		const found = await client.query('SELECT id, status, kit FROM public.codes WHERE code = $1 FOR UPDATE', [code]);
		const row = found.rows[0];
		if (row === undefined || row.status !== 'unused') {
			await client.query('ROLLBACK');
			if (row === undefined) return { outcome: 'unknown_code' };
			return { outcome: row.status === 'revoked' ? 'revoked' : 'already_redeemed' };
		}
		await client.query("UPDATE public.codes SET status = 'redeemed' WHERE status = 'unused'");
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
