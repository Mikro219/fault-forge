// False fix: the claim is atomic, but the audit row is gone. Fails the audit accounting rule.
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
			"UPDATE public.codes SET status = 'redeemed' WHERE code = $1 AND status = 'unused' RETURNING id, kit",
			[code]
		);
		const row = claimed.rows[0];
		if (row === undefined) {
			await client.query('ROLLBACK');
			const found = await client.query('SELECT status FROM public.codes WHERE code = $1', [code]);
			const status = found.rows[0]?.status;
			if (status === undefined) return { outcome: 'unknown_code' };
			return { outcome: status === 'revoked' ? 'revoked' : 'already_redeemed' };
		}
		const items = await loadKit(client, row.kit);
		await client.query('INSERT INTO public.redemptions (code_id, user_id) VALUES ($1, $2)', [row.id, user]);
		await client.query('COMMIT');
		return { outcome: 'redeemed', kit: row.kit, items };
	} catch (error) {
		await client.query('ROLLBACK').catch(() => undefined);
		throw error;
	} finally {
		client.release();
	}
}
