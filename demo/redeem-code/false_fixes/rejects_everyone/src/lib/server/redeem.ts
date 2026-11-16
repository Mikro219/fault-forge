// False fix: nobody is ever double-served, because nobody is served. Fails the liveness rule.
import { pool } from './db.ts';

export type RedeemResult =
	| { outcome: 'redeemed'; kit: string; items: string[] }
	| { outcome: 'already_redeemed' | 'revoked' | 'unknown_code' };

export async function redeem(code: string, _user: string): Promise<RedeemResult> {
	const found = await pool().query('SELECT status FROM public.codes WHERE code = $1', [code]);
	const status = found.rows[0]?.status;
	if (status === undefined) return { outcome: 'unknown_code' };
	return { outcome: status === 'revoked' ? 'revoked' : 'already_redeemed' };
}
