// `node tools/migrate.ts`: applies the pending files of migrations/ as the database's owner, then
// exits. It is a step of its own, run before the app, the way a real project runs it: the owner's
// URL is in MIGRATION_DATABASE_URL and is never given to the app, which logs in as redeem_app with
// DATABASE_URL and has only what the migrations grant it.
//
// Output is fixed phrases, file names and SQLSTATEs: never a URL or a server message.
import { createHash } from 'node:crypto';
import { readdirSync, readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import pg from 'pg';

const APP_ROLE = 'redeem_app';
// One fixed key ("redeemmg"), so two runners on one database take turns.
const LOCK_KEY = '8243105079627705703';
const MIGRATIONS = fileURLToPath(new URL('../migrations', import.meta.url));
const LEDGER =
	'CREATE TABLE IF NOT EXISTS public.schema_migrations (' +
	'name text PRIMARY KEY, sha256 text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())';

function fail(what: string, error?: unknown): number {
	const code = (error as { code?: unknown } | undefined)?.code;
	const sqlstate = typeof code === 'string' && /^[0-9A-Z]{5}$/.test(code) ? ` (SQLSTATE ${code})` : '';
	process.stderr.write(`migrate: ${what}${sqlstate}\n`);
	return 1;
}

function roleOf(url: string | undefined): string | undefined {
	try {
		return url === undefined ? undefined : decodeURIComponent(new URL(url).username);
	} catch {
		return undefined;
	}
}

async function appCanLogIn(url: string): Promise<boolean> {
	const client = new pg.Client({ connectionString: url, connectionTimeoutMillis: 10_000 });
	try {
		await client.connect();
		await client.query('SELECT 1');
		return true;
	} catch {
		return false;
	} finally {
		await client.end().catch(() => undefined);
	}
}

async function main(): Promise<number> {
	const ownerUrl = process.env['MIGRATION_DATABASE_URL'];
	const appUrl = process.env['DATABASE_URL'];
	if (ownerUrl === undefined || appUrl === undefined) return fail('MIGRATION_DATABASE_URL and DATABASE_URL are required');
	if (roleOf(appUrl) !== APP_ROLE || roleOf(ownerUrl) === APP_ROLE) {
		return fail(`DATABASE_URL must be for role ${APP_ROLE} and MIGRATION_DATABASE_URL for the owner`);
	}
	const files = readdirSync(MIGRATIONS)
		.filter((file) => file.endsWith('.sql'))
		.sort()
		.map((file) => {
			const bytes = readFileSync(`${MIGRATIONS}/${file}`);
			return { name: file.slice(0, -'.sql'.length), sha256: createHash('sha256').update(bytes).digest('hex'), sql: bytes.toString() };
		});

	const client = new pg.Client({ connectionString: ownerUrl, connectionTimeoutMillis: 10_000 });
	try {
		await client.connect();
	} catch (error) {
		await client.end().catch(() => undefined);
		return fail('the owner could not connect', error);
	}
	try {
		await client.query('SELECT pg_advisory_lock($1::bigint)', [LOCK_KEY]);
		// The migrations grant to the app role by name, so it must exist and log in before any runs.
		const role = await client.query('SELECT 1 FROM pg_roles WHERE rolname = $1 AND rolcanlogin', [APP_ROLE]);
		if (role.rowCount === 0 || !(await appCanLogIn(appUrl))) return fail(`role ${APP_ROLE} is missing or cannot log in`);
		await client.query(LEDGER);
		const ledger = (await client.query('SELECT name, sha256 FROM public.schema_migrations ORDER BY name')).rows;
		for (const [index, row] of ledger.entries()) {
			const file = files[index];
			// Forward-only: an applied file is never edited, renamed or removed.
			if (file === undefined || file.name !== row.name || file.sha256 !== row.sha256) {
				return fail(`history mismatch at ${String(row.name).slice(0, 80)}; nothing applied`);
			}
		}
		for (const file of files.slice(ledger.length)) {
			try {
				await client.query('BEGIN');
				await client.query(file.sql);
				await client.query('INSERT INTO public.schema_migrations (name, sha256) VALUES ($1, $2)', [file.name, file.sha256]);
				await client.query('COMMIT');
			} catch (error) {
				await client.query('ROLLBACK').catch(() => undefined);
				return fail(`${file.name} failed; rolled back`, error);
			}
			process.stdout.write(`migrate: applied ${file.name}\n`);
		}
		if (files.length === ledger.length) process.stdout.write(`migrate: up to date (${ledger.length} applied)\n`);
		return 0;
	} catch (error) {
		return fail('failed', error);
	} finally {
		// Ending the session releases its advisory lock.
		await client.end().catch(() => undefined);
	}
}

process.exitCode = await main();
