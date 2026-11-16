// POST {code, user}. 200 redeemed, 409 already redeemed, 410 revoked, 404 unknown code.
import { json } from '@sveltejs/kit';
import { redeem } from '#lib/server/redeem.ts';
import type { RequestHandler } from './$types';

const REFUSALS = {
	already_redeemed: 409,
	revoked: 410,
	unknown_code: 404
} as const;

export const POST: RequestHandler = async ({ request }) => {
	const body: unknown = await request.json().catch(() => null);
	const { code, user } = (body ?? {}) as { code?: unknown; user?: unknown };
	if (typeof code !== 'string' || typeof user !== 'string' || code.length > 64 || user.length > 64) {
		return json({ error: 'bad_request' }, { status: 400 });
	}
	const result = await redeem(code, user);
	if (result.outcome === 'redeemed') {
		return json({ status: 'redeemed', kit: result.kit, items: result.items.length });
	}
	return json({ error: result.outcome }, { status: REFUSALS[result.outcome] });
};
