// Loads what a kit unlocks. One query for the positions and then one per item: the usual N+1
// pattern, left as it is. It is slow, not wrong, and it is real work a redemption does.
import type pg from 'pg';

type Queryable = Pick<pg.Pool, 'query'>;

export async function loadKit(db: Queryable, kit: string): Promise<string[]> {
	const positions = await db.query('SELECT position FROM public.kit_items WHERE kit = $1 ORDER BY position', [kit]);
	const names: string[] = [];
	for (const row of positions.rows) {
		const item = await db.query('SELECT name FROM public.kit_items WHERE kit = $1 AND position = $2', [
			kit,
			row.position
		]);
		names.push(item.rows[0].name);
	}
	return names;
}
