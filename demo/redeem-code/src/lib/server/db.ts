// One pg pool for the process, from DATABASE_URL. Made on first use, so the build needs no database.
import pg from 'pg';

let shared: pg.Pool | undefined;

export function pool(): pg.Pool {
	shared ??= new pg.Pool({ connectionString: process.env['DATABASE_URL'], max: 10 });
	return shared;
}
