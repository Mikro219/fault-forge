// Applies the one migration before the server listens, so /healthz answers only once the tables
// exist. Nothing runs while building: the build has no database.
import { readFile } from 'node:fs/promises';
import { building } from '$app/env';
import type { ServerInit } from '@sveltejs/kit/hooks';
import { pool } from '#lib/server/db.ts';

export const init: ServerInit = async () => {
	if (building) return;
	const sql = await readFile('migrations/0000_init.sql', 'utf8');
	await pool().query(sql);
};
