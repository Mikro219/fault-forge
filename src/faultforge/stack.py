"""Runs a multi-service target: an app image, Postgres and, if the stack names one, Redis, sealed together.

Postgres owns a network namespace with no network, and everything else joins it, so 127.0.0.1 reaches
the other services and nothing else exists. Every container has the same limits as a Python target's.
Secrets are random for each run, or fixed test values in the stack's definition. Nothing is read
from a repo's own env files. The deployed-code hash is the image's ID, and the host reads the database
with psql as a hooks role the app can't log in as, and the hooks helper starts before the app, so
it can't take the helper's port. A run where a trusted service died can't pass the gate.

The app owns its own database, so like any Python target it can still edit its data, including
replacing a table with a view. Client counts are measured by the runner, but a cheating app could
also keep its state in memory. So a verified fix is strong evidence against honest bugs, and not
proof against code written to fool the check.

The image is built beforehand, because a real build doesn't fit in a sandbox's wall limit. It's
only as trustworthy as that build, and the app is untrusted here. For a stack with a `build:`
section, node_build.py makes the image from source, and the deployed-code hash is then the host's
hash of that source (StackSpec.code_sha256), because every image build has a new ID.

Three things hold for every stack (docs/PLAN.md M11). Postgres asks for a password on every TCP
login, so the app, which shares its loopback, can't log in as the administrator; the unix socket
inside the postgres container stays open to the host's docker exec. Every trusted session pins its
search_path, so an object the app defines in its own schema with a built-in's name and argument
types can't stand in for it. An overload with closer argument types still could, so FaultForge's
own statements call pg_catalog by name, and the inventory's session has no other schema on its
path. The project's reset and state SQL are the project's: the pin is all they have. And
the hooks helper answers only a caller that holds this run's token, which the runner has and the
app doesn't; that stops an app calling reset by accident, and is not a defence against an app that
owns its schema and is written to cheat.

A stack with a `prestart:` section has two database roles instead of one. An owner role owns the
database and its public schema, and an app role owns nothing: it has only what the project's own
migrations grant it. The prestart command (a project's migration step) runs once in the app image
before the hooks helper and the app, sealed exactly as the app is, and it alone gets the owner's
login. It never gets the hooks token or the administrator's password, and the app never gets the
owner's. A prestart that exits non-zero or outlives its limit stops the run: target_start_failed.
Neither role is a superuser, so neither can run COPY ... PROGRAM or read a server file. What the
owner can still do is whatever a schema's owner can: it is project code, and a migration can
attach a trigger, a rule or a view to a table the pinned SQL reads or resets.

The pinned SQL (reset and state, in the helper and from the host) runs as faultforge_hooks, an
ordinary role that is a member of the role that owns the tables. So a trigger, rule or view function
that fires in one of those sessions has the table owner's rights and no superuser's: no COPY ...
PROGRAM and no server file. The administrator is used for the role setup and for locking the owner
only. A member of the table's owner is subject to a forced row-security policy, which a superuser
never was, so those sessions set row_security = off: a query a policy would filter fails instead,
and the state read is unavailable rather than forged.

Each run also records a catalog inventory: the triggers, rules, views, row-security policies,
functions, operators, casts, generated columns, partitioned, foreign and child tables and event
triggers of the app's schemas, each with a digest of its definition. They are what can make the
state SQL read something other than the tables' rows. It is read when the runner is done, for every
stack. A stack with a prestart is also read once the migrations are in, before anything of the
project's has run as the hooks role: a check constraint or a default can call a function during
the first reset, and that function could make a view. What the second read has and the first
hasn't appeared during the run, and is recorded as that. The inventory is facts for a later
comparison (mcp_server/tools.py), and never changes a verdict.
"""

import dataclasses
import hashlib
import json
import os
import re
import secrets
import subprocess
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml
from pydantic import Field, field_validator, model_validator

from faultforge import config as config_module, stack_hooks
from faultforge.hooks import HOOKS_TOKEN_ENV
from faultforge.lifecycle import RunOptions
from faultforge.models import Contract, Report
from faultforge.node_build import PROJECT, BuildConfig, is_plain_node_command
from faultforge.sandbox import (
    DEFAULT_POLICY,
    MAX_RUNNER_STDOUT_BYTES,
    MAX_STATE_OUTPUT_BYTES,
    STATE_EXEC_TIMEOUT_S,
    ContainerFacts,
    ContainerLimits,
    SandboxError,
    SandboxPolicy,
    SandboxResult,
    StateVerification,
    _common_args,
    _docker,
    _DockerTimeout,
    _env_args,
    _prepare_project,
    _Session,
    _require_valid_verifier,
    _stderr,
    expiry_unix,
    parse_state_output,
    reap_stale,
    run_session,
    runner_argv,
)

HOOKS_IMAGE = "faultforge-stack-hooks:dev"
POSTGRES_USER = "70:70"
REDIS_USER = "999:1000"
HOOKS_USER = "65534:65534"
ADMIN_ROLE = "faultforge_admin"
# What the pinned reset and state SQL run as: an ordinary role, a member of the tables' owner.
HOOKS_ROLE = "faultforge_hooks"
READY_WAIT_S = 30.0
READY_POLL_S = 0.25
# TCP logins need the password. The unix socket keeps initdb's default, trust, and only something
# inside the postgres container can reach it.
POSTGRES_INITDB_ARGS = "--auth-host=scram-sha-256"
# Built-in names resolve to pg_catalog first, whatever the app defines in its own schema.
ADMIN_SEARCH_PATH = "pg_catalog, public"
HEX_PASSWORD = re.compile(r"[a-f0-9]{16,64}")
# The catalog inventory: how many objects one run records, how many characters their lines may add
# up to (the list is stored in run.json), how long one part of a line may be, and how much output
# is read. More than this is a failed read with its own reason, never a cut list.
MAX_SCHEMA_OBJECTS = 1000
MAX_SCHEMA_CHARS = 80_000
MAX_SCHEMA_NAME = 80
MAX_SCHEMA_OUTPUT_BYTES = 2 * 1024 * 1024
SCHEMA_TOO_MANY = "too_many_objects"
SCHEMA_KINDS = frozenset({
    "trigger", "rule", "view", "materialized_view", "policy", "row_security", "row_security_forced",
    "function", "security_definer_function", "procedure", "aggregate", "operator", "cast", "generated_column",
    "partitioned_table", "foreign_table", "child_table", "event_trigger",
})
SCHEMA_DIGEST = re.compile(r"[a-f0-9]{12}")
_UNPRINTABLE = re.compile(r"[^\x20-\x7e]")


def admin_conninfo(stack: "StackConfig") -> str:
    """psql's -d value for an admin session: the database, and the search_path as a connection
    option, which outranks a default the app sets on its own database or role."""
    return f"dbname={stack.database_name} options=-csearch_path={ADMIN_SEARCH_PATH.replace(' ', '')}"


def hooks_conninfo(stack: "StackConfig", *extra: str, search_path: str = ADMIN_SEARCH_PATH) -> str:
    """psql's -d value for a session as the hooks role. The search_path is pinned as for the
    administrator. row_security = off makes a query that a row-security policy would filter fail,
    so a policy can't hide rows from the check without it showing. Connection options outrank a
    default set on the database or on the role."""
    options = [f"-csearch_path={search_path.replace(' ', '')}", "-crow_security=off", *extra]
    return f"dbname={stack.database_name} options='{' '.join(options)}'"


# Below 16, a role with ADMIN OPTION on itself can grant itself to others, so a migration could hand
# the owner to the app. The server is asked, not the image tag: a tag can say anything.
MIN_POSTGRES_MAJOR = 16


def postgres_version_problem(output: bytes | str) -> str | None:
    """Why the server's `SHOW server_version_num` rules this stack out, or None. Unreadable output
    is a problem too: the check fails closed."""
    text = output.decode("ascii", "replace") if isinstance(output, bytes) else output
    text = text.strip()
    if not text.isdigit():
        return "the database server's version couldn't be read"
    if int(text) < MIN_POSTGRES_MAJOR * 10000:
        return f"the database server is older than postgres {MIN_POSTGRES_MAJOR}, which FaultForge needs"
    return None


def admin_psql_argv(container: str, stack: "StackConfig", sql: str) -> list[str]:
    """One statement list as the administrator, over the socket inside the postgres container. For
    the role setup and for locking the owner: never for the project's SQL or its tables."""
    return [
        "exec", container, "psql", "-X", "-U", ADMIN_ROLE, "-d", admin_conninfo(stack), "-v", "ON_ERROR_STOP=1", "-c", sql,
    ]

# Measured in the SportsDeck spike: 114 MiB, 108 MiB and 15 MiB after load. Start-up runs migrations,
# so the app's limit leaves room for those too.
STACK_POLICY = dataclasses.replace(
    DEFAULT_POLICY,
    target=ContainerLimits(memory_mb=1024, cpus=1.0, pids=256, tmpfs_mb=64, log_mb=4),
    wall_limit_s=150.0,
    wait_for_target_s=30.0,
)
PRESTART_ROLE = "prestart"
# What a failed prestart's output is cut to. It is project text, and a result quotes it on one line.
PRESTART_LOG_CHARS = 200
# Names the sandbox sets itself, so a prestart's owner URL can never take one of them over.
RESERVED_ENV = frozenset({
    "DATABASE_URL", "POSTGRES_HOST", "POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB", "REDIS_URL", "HOME", "PORT", "HOSTNAME",
})
POSTGRES_LIMITS = ContainerLimits(memory_mb=512, cpus=1.0, pids=128, tmpfs_mb=16, log_mb=1)
REDIS_LIMITS = ContainerLimits(memory_mb=128, cpus=0.5, pids=32, tmpfs_mb=8, log_mb=1)
HOOKS_LIMITS = ContainerLimits(memory_mb=128, cpus=0.5, pids=32, tmpfs_mb=8, log_mb=1)


class PrestartConfig(Contract):
    """The `prestart:` section of stack.yaml: one command run in the app image before the hooks
    helper and the app, as the database's owner. It is how a project's migrations are applied when
    they are a step of their own. There is no shell: the command is an argument list for node."""

    command: list[str] = Field(min_length=1, max_length=16)
    owner_role: str = Field(pattern=r"^[a-z][a-z0-9_]{0,30}$")
    # The variable that carries the owner's connection URL. The app's own URL is always DATABASE_URL.
    owner_url_env: str = Field(default="MIGRATION_DATABASE_URL", pattern=r"^[A-Z][A-Z0-9_]{0,63}$")
    timeout_s: float = Field(default=30.0, ge=1.0, le=60.0)

    @field_validator("owner_role")
    @classmethod
    def owner_role_is_not_reserved(cls, value: str) -> str:
        if value.startswith("pg_") or value in ("public", "postgres", "none", "current_user", "session_user", ADMIN_ROLE, HOOKS_ROLE):
            raise ValueError("prestart.owner_role must not be a reserved or built-in role name")
        return value

    @field_validator("command")
    @classmethod
    def command_is_a_plain_node_command(cls, value: list[str]) -> list[str]:
        if not is_plain_node_command(value):
            raise ValueError("prestart.command must be an argument list that begins with node, such as [node, tools/migrate.ts]")
        return value

    @field_validator("owner_url_env")
    @classmethod
    def owner_url_env_is_its_own_name(cls, value: str) -> str:
        if value in RESERVED_ENV or value.startswith("FF_"):
            raise ValueError("prestart.owner_url_env can't be a name the sandbox sets itself, such as DATABASE_URL")
        return value


class StackConfig(Contract):
    """A stack's definition. It's trusted: it comes from this repo, never from a request."""

    postgres_image: str
    redis_image: str | None = None
    # The prebuilt app image the MCP server runs for this stack. The CLI and tests pass their own.
    app_image: str | None = None
    database_user: str = Field(pattern=r"^[a-z][a-z0-9_]{0,30}$")
    database_name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,30}$")
    app_port: int = 8000
    app_user: str = Field(pattern=r"^[0-9]+:[0-9]+$")
    app_health_path: str
    app_writable: list[str] = Field(default_factory=list)
    hooks_port: int = 8001
    env: dict[str, str] = Field(default_factory=dict, description="Fixed test values only, never real secrets")
    reset_sql: list[str] = Field(min_length=1)
    state_sql: dict[str, str] = Field(min_length=1)
    # How to build the app from source (node_build.py). Without it the stack runs a prebuilt image only.
    build: BuildConfig | None = None
    # A step before the app, run as the database's owner. With it, database_user is a lesser role.
    prestart: PrestartConfig | None = None

    @field_validator("database_user")
    @classmethod
    def database_user_is_not_faultforges_own(cls, value: str) -> str:
        if value in (ADMIN_ROLE, HOOKS_ROLE):
            raise ValueError("database_user must not be one of FaultForge's own roles")
        return value

    @model_validator(mode="after")
    def prestart_roles_are_distinct(self) -> "StackConfig":
        step = self.prestart
        if step is not None:
            if step.owner_role in (self.database_user, ADMIN_ROLE, HOOKS_ROLE):
                raise ValueError("prestart.owner_role must differ from database_user and from FaultForge's own administrator")
            if step.owner_url_env in self.env:
                raise ValueError("prestart.owner_url_env can't be a name that env already sets")
        return self


_PORT_BOUND = "import socket; socket.create_connection(('127.0.0.1', {port}), 1).close()"
# Run inside the helper's container: a call without the run's token must get 403. A stale helper
# image, built before the token existed, answers it, and the run is refused instead of trusted.
_TOKEN_DEMANDED = (
    "import sys, urllib.request, urllib.error\n"
    "try:\n"
    "    urllib.request.urlopen('http://127.0.0.1:{port}/__ff/state', timeout=3)\n"
    "except urllib.error.HTTPError as error:\n"
    "    sys.exit(0 if error.code == 403 else 3)\n"
    "except Exception:\n"
    "    sys.exit(4)\n"
    "sys.exit(5)\n"
)


# Run inside the helper's container: the helper file in the image must be this FaultForge's, byte
# for byte. The image is built from the same file, so a difference means the image is stale.
HELPER_DIFFERS, HELPER_UNREADABLE = 6, 7
_HELPER_IS_CURRENT = (
    "import hashlib, sys\n"
    "try:\n"
    "    with open('/opt/stack_hooks.py', 'rb') as helper:\n"
    "        found = hashlib.sha256(helper.read()).hexdigest()\n"
    "except OSError:\n"
    f"    sys.exit({HELPER_UNREADABLE})\n"
    f"sys.exit(0 if found == sys.argv[1] else {HELPER_DIFFERS})\n"
)


def helper_sha256() -> str:
    """Of the hooks helper's source as this FaultForge has it."""
    return hashlib.sha256(Path(stack_hooks.__file__).read_bytes()).hexdigest()


def load_stack(path: Path) -> StackConfig:
    return StackConfig.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))


@dataclass(frozen=True)
class StackSpec:
    app_image: str
    stack: StackConfig
    config_path: Path
    # For an image FaultForge built from source: the host's hash of that source. When set, it is the
    # run's deployed_code_sha256; otherwise the image's ID is hashed, as for any prebuilt image.
    code_sha256: str | None = None

    @property
    def state_verifier(self) -> Mapping[str, str]:
        return self.stack.state_sql

    def deployed_code(self, image_id: str) -> str:
        """What the host knows ran: the hash of the source it built, or of a prebuilt image's ID."""
        return self.code_sha256 or hashlib.sha256(image_id.encode()).hexdigest()


def psql_state_argv(container: str, stack: StackConfig, queries: Mapping[str, str]) -> list[str]:
    """One statement, so the values are read together. Names were checked as plain lowercase names.
    The function that gathers them is called in pg_catalog by name: an overload in the app's schema
    with closer argument types would otherwise be chosen over it, whatever the search_path."""
    fields = ", ".join(f"'{name}', ({sql})" for name, sql in queries.items())
    return [
        "exec", container, "psql", "-X", "-U", HOOKS_ROLE, "-d", hooks_conninfo(stack),
        "-tA", "-v", "ON_ERROR_STOP=1", "-c", f"SELECT pg_catalog.json_build_object({fields})",
    ]


def tables_owner(stack: StackConfig) -> str:
    """The role that owns what the project creates: the prestart's owner, or the app's own role."""
    return stack.database_user if stack.prestart is None else stack.prestart.owner_role


def setup_role_sql(stack: StackConfig, password: str, owner_password: str = "", *, hooks_password: str) -> str:
    """The app gets an ordinary role that owns only its own database, so it has no superuser powers
    such as COPY FROM PROGRAM. The names were checked as plain lowercase, and the passwords are hex.

    With a prestart step there are two ordinary roles. The owner owns the database and its public
    schema, and the app role owns nothing: PUBLIC loses CREATE on the schema (a default before
    Postgres 15), so the app has only what the project's migrations grant it. Neither can create a
    role, so neither can hand itself pg_execute_server_program.

    Last comes the hooks role, which the pinned reset and state SQL run as. It is as ordinary as the
    others, with a password of its own, and a member of the role that owns the tables: membership
    and not table grants, because TRUNCATE ... RESTART IDENTITY needs ownership, and the tables
    don't exist yet. The grant is one way: the owner gains nothing from it."""
    role, database = stack.database_user, stack.database_name
    ordinary = "LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE"
    if not HEX_PASSWORD.fullmatch(hooks_password) or hooks_password in (password, owner_password):
        raise SandboxError("invalid_spec", "the hooks role needs a password of its own")
    hooks = f" CREATE ROLE {HOOKS_ROLE} {ordinary} PASSWORD '{hooks_password}'; GRANT {tables_owner(stack)} TO {HOOKS_ROLE};"
    if stack.prestart is None:
        return (
            f"CREATE ROLE {role} {ordinary} PASSWORD '{password}'; "
            f"ALTER DATABASE {database} OWNER TO {role}; "
            f"ALTER SCHEMA public OWNER TO {role};"
        ) + hooks
    if not HEX_PASSWORD.fullmatch(owner_password) or owner_password == password:
        raise SandboxError("invalid_spec", "a stack with a prestart step needs an owner password of its own")
    owner = stack.prestart.owner_role
    return (
        f"CREATE ROLE {owner} {ordinary} PASSWORD '{owner_password}'; "
        f"CREATE ROLE {role} {ordinary} PASSWORD '{password}'; "
        f"ALTER DATABASE {database} OWNER TO {owner}; "
        f"ALTER SCHEMA public OWNER TO {owner}; "
        f"REVOKE CREATE ON SCHEMA public FROM PUBLIC;"
    ) + hooks


# The app's schemas: everything but the system's. A schema name can't begin with pg_ unless the
# server made it.
_APP_SCHEMA = "n.nspname <> 'information_schema' AND n.nspname !~ '^pg_'"
_APP_PARENT = _APP_SCHEMA.replace("n.nspname", "pn.nspname")
# The first OID the server gives to anything made after initdb.
_FIRST_USER_OID = 16384
# One row for each object that can change what a statement over the app's tables reads or does:
# [kind, name, table, digest]. The digest covers the object's definition as the server prints it,
# never an OID, so the same migrations give the same rows in every run. Constraint triggers that
# the server makes for a foreign key are internal and left out, and so is the _RETURN rule every
# view has: the view itself is a row. Only catalog tables and the server's own deparse functions are
# read, so no project code runs here.
#
# The session's search_path is pg_catalog alone and every function is called there by name. With
# any other schema on the path, an aggregate public.json_agg(json) or a function with closer
# argument types than the built-in's would be chosen over it and could return an empty list.
# Operators and casts can't be named that way everywhere, which is why the path matters: with
# pg_catalog alone on it, no operator of the app's is visible, and a cast between built-in types
# can only be made by the types' owner, which no project role is.
SCHEMA_INVENTORY_SQL = f"""
SELECT COALESCE(pg_catalog.json_agg(pg_catalog.json_build_array(kind, name, tbl, pg_catalog.substr(pg_catalog.md5(
           pg_catalog.concat_ws(pg_catalog.chr(10), kind, name, tbl, def)), 1, 12)) ORDER BY kind, name, tbl), '[]'::json)
FROM (
    SELECT 'trigger'::text AS kind, t.tgname::text AS name, pg_catalog.concat(n.nspname, '.', c.relname) AS tbl,
           pg_catalog.concat_ws(pg_catalog.chr(10), pg_catalog.pg_get_triggerdef(t.oid), t.tgenabled::text,
                                pg_catalog.concat(pn.nspname, '.', p.proname), pg_catalog.md5(p.prosrc), p.probin,
                                p.prosecdef::text, pg_catalog.array_to_string(p.proconfig, ',')) AS def
    FROM pg_catalog.pg_trigger t
    JOIN pg_catalog.pg_class c ON c.oid = t.tgrelid
    JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
    JOIN pg_catalog.pg_proc p ON p.oid = t.tgfoid
    JOIN pg_catalog.pg_namespace pn ON pn.oid = p.pronamespace
    WHERE NOT t.tgisinternal AND {_APP_SCHEMA}
    UNION ALL
    SELECT 'rule', r.rulename::text, pg_catalog.concat(n.nspname, '.', c.relname),
           pg_catalog.concat_ws(pg_catalog.chr(10), pg_catalog.pg_get_ruledef(r.oid), r.ev_enabled::text)
    FROM pg_catalog.pg_rewrite r
    JOIN pg_catalog.pg_class c ON c.oid = r.ev_class
    JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
    WHERE NOT (r.rulename = '_RETURN' AND c.relkind IN ('v', 'm')) AND {_APP_SCHEMA}
    UNION ALL
    SELECT CASE c.relkind WHEN 'm' THEN 'materialized_view' ELSE 'view' END, pg_catalog.concat(n.nspname, '.', c.relname), '',
           pg_catalog.concat_ws(pg_catalog.chr(10), pg_catalog.pg_get_viewdef(c.oid), pg_catalog.array_to_string(c.reloptions, ','))
    FROM pg_catalog.pg_class c
    JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relkind IN ('v', 'm') AND {_APP_SCHEMA}
    UNION ALL
    SELECT 'policy', p.polname::text, pg_catalog.concat(n.nspname, '.', c.relname),
           pg_catalog.concat_ws(pg_catalog.chr(10), p.polcmd::text, p.polpermissive::text,
                                pg_catalog.pg_get_expr(p.polqual, p.polrelid), pg_catalog.pg_get_expr(p.polwithcheck, p.polrelid),
                                (SELECT pg_catalog.string_agg(COALESCE(r.rolname::text, 'public'), ',' ORDER BY COALESCE(r.rolname::text, 'public'))
                                 FROM pg_catalog.unnest(p.polroles) AS member(id) LEFT JOIN pg_catalog.pg_roles r ON r.oid = member.id))
    FROM pg_catalog.pg_policy p
    JOIN pg_catalog.pg_class c ON c.oid = p.polrelid
    JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
    WHERE {_APP_SCHEMA}
    UNION ALL
    SELECT CASE WHEN c.relforcerowsecurity THEN 'row_security_forced' ELSE 'row_security' END, pg_catalog.concat(n.nspname, '.', c.relname), '',
           pg_catalog.concat_ws(',', c.relrowsecurity::text, c.relforcerowsecurity::text)
    FROM pg_catalog.pg_class c
    JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
    WHERE (c.relrowsecurity OR c.relforcerowsecurity) AND {_APP_SCHEMA}
    UNION ALL
    SELECT CASE WHEN p.prosecdef THEN 'security_definer_function' WHEN p.prokind = 'a' THEN 'aggregate'
                WHEN p.prokind = 'p' THEN 'procedure' ELSE 'function' END,
           pg_catalog.concat(n.nspname, '.', p.proname, '(', pg_catalog.pg_get_function_identity_arguments(p.oid), ')'), '',
           pg_catalog.concat_ws(pg_catalog.chr(10), pg_catalog.md5(p.prosrc), pg_catalog.pg_get_function_sqlbody(p.oid), p.probin,
                                l.lanname::text, pg_catalog.pg_get_userbyid(p.proowner)::text, pg_catalog.array_to_string(p.proconfig, ','),
                                pg_catalog.pg_get_function_result(p.oid), p.prokind::text, p.prosecdef::text, p.provolatile::text,
                                p.proretset::text, p.proisstrict::text, a.aggtransfn::regproc::text, a.aggfinalfn::regproc::text,
                                a.aggcombinefn::regproc::text, a.agginitval, pg_catalog.format_type(a.aggtranstype, NULL))
    FROM pg_catalog.pg_proc p
    JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
    JOIN pg_catalog.pg_language l ON l.oid = p.prolang
    LEFT JOIN pg_catalog.pg_aggregate a ON a.aggfnoid = p.oid
    WHERE {_APP_SCHEMA}
    UNION ALL
    SELECT 'operator',
           pg_catalog.concat(n.nspname, '.', o.oprname, '(', pg_catalog.format_type(NULLIF(o.oprleft, 0), NULL), ',',
                             pg_catalog.format_type(NULLIF(o.oprright, 0), NULL), ')'), '',
           pg_catalog.concat_ws(pg_catalog.chr(10), o.oprcode::regproc::text, pg_catalog.format_type(o.oprresult, NULL),
                                o.oprkind::text, o.oprcom::regoperator::text, o.oprnegate::regoperator::text)
    FROM pg_catalog.pg_operator o
    JOIN pg_catalog.pg_namespace n ON n.oid = o.oprnamespace
    WHERE o.oid >= {_FIRST_USER_OID}
    UNION ALL
    SELECT 'cast', pg_catalog.concat(pg_catalog.format_type(k.castsource, NULL), ' AS ', pg_catalog.format_type(k.casttarget, NULL)), '',
           pg_catalog.concat_ws(pg_catalog.chr(10), k.castfunc::regproc::text, k.castcontext::text, k.castmethod::text)
    FROM pg_catalog.pg_cast k
    WHERE k.oid >= {_FIRST_USER_OID}
    UNION ALL
    SELECT 'generated_column', a.attname::text, pg_catalog.concat(n.nspname, '.', c.relname),
           pg_catalog.concat_ws(pg_catalog.chr(10), a.attgenerated::text, pg_catalog.pg_get_expr(d.adbin, d.adrelid))
    FROM pg_catalog.pg_attribute a
    JOIN pg_catalog.pg_class c ON c.oid = a.attrelid
    JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
    LEFT JOIN pg_catalog.pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
    WHERE a.attgenerated <> '' AND NOT a.attisdropped AND {_APP_SCHEMA}
    UNION ALL
    SELECT CASE c.relkind WHEN 'p' THEN 'partitioned_table' ELSE 'foreign_table' END, pg_catalog.concat(n.nspname, '.', c.relname), '',
           pg_catalog.concat_ws(pg_catalog.chr(10), CASE WHEN c.relkind = 'p' THEN pg_catalog.pg_get_partkeydef(c.oid) END,
                                (SELECT pg_catalog.concat_ws(',', s.srvname::text, pg_catalog.array_to_string(f.ftoptions, ','))
                                 FROM pg_catalog.pg_foreign_table f JOIN pg_catalog.pg_foreign_server s ON s.oid = f.ftserver
                                 WHERE f.ftrelid = c.oid))
    FROM pg_catalog.pg_class c
    JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relkind IN ('p', 'f') AND {_APP_SCHEMA}
    UNION ALL
    SELECT 'child_table', pg_catalog.concat(n.nspname, '.', c.relname), pg_catalog.concat(pn.nspname, '.', pc.relname),
           pg_catalog.concat_ws(pg_catalog.chr(10), c.relispartition::text, pg_catalog.pg_get_expr(c.relpartbound, c.oid), i.inhseqno::text)
    FROM pg_catalog.pg_inherits i
    JOIN pg_catalog.pg_class c ON c.oid = i.inhrelid
    JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
    JOIN pg_catalog.pg_class pc ON pc.oid = i.inhparent
    JOIN pg_catalog.pg_namespace pn ON pn.oid = pc.relnamespace
    WHERE c.relkind IN ('r', 'p', 'f') AND (({_APP_SCHEMA}) OR ({_APP_PARENT}))
    UNION ALL
    SELECT 'event_trigger', e.evtname::text, '',
           pg_catalog.concat_ws(pg_catalog.chr(10), e.evtevent::text, e.evtenabled::text, e.evtfoid::regproc::text,
                                pg_catalog.array_to_string(e.evttags, ','))
    FROM pg_catalog.pg_event_trigger e
) AS found
""".strip()
# Nothing but pg_catalog: see above.
SCHEMA_SEARCH_PATH = "pg_catalog"


def schema_inventory_argv(container: str, stack: StackConfig) -> list[str]:
    """As the hooks role, in a read-only transaction with a short limit: the catalogs are readable
    by every role, so this needs no more than that."""
    conninfo = hooks_conninfo(stack, "-cdefault_transaction_read_only=on", "-cstatement_timeout=5000", search_path=SCHEMA_SEARCH_PATH)
    return [
        "exec", container, "psql", "-X", "-U", HOOKS_ROLE, "-d", conninfo, "-tA", "-v", "ON_ERROR_STOP=1", "-c", SCHEMA_INVENTORY_SQL,
    ]


class SchemaTooLarge(ValueError):
    """More objects, or more output, than one run records."""


def schema_fits(lines: list[str]) -> bool:
    return len(lines) <= MAX_SCHEMA_OBJECTS and sum(len(line) for line in lines) <= MAX_SCHEMA_CHARS


def _schema_text(value: str) -> str:
    """An object's name is the project's text: one line, printable, cut short."""
    return _UNPRINTABLE.sub("?", " ".join(value.split()))[:MAX_SCHEMA_NAME]


def parse_schema_inventory(output: bytes) -> list[str]:
    """The inventory as sorted lines, `kind name on table #digest`. Anything but the exact shape the
    query returns is a ValueError, and more than one run records is a SchemaTooLarge: a list that
    can't be trusted whole is not recorded in part."""
    if len(output) > MAX_SCHEMA_OUTPUT_BYTES:
        raise SchemaTooLarge("more output than is read")
    rows = json.loads(output.decode("utf-8"))
    if not isinstance(rows, list):
        raise ValueError("not a list of rows")
    if len(rows) > MAX_SCHEMA_OBJECTS:
        raise SchemaTooLarge("more rows than MAX_SCHEMA_OBJECTS")
    lines = set()
    for row in rows:
        if not (isinstance(row, list) and len(row) == 4 and all(isinstance(item, str) for item in row)):
            raise ValueError("not a row of four strings")
        kind, name, table, digest = row
        if kind not in SCHEMA_KINDS or not name or not SCHEMA_DIGEST.fullmatch(digest):
            raise ValueError("an unknown kind, an empty name or a malformed digest")
        lines.add(f"{kind} {_schema_text(name)}" + (f" on {_schema_text(table)}" if table else "") + f" #{digest}")
    if not schema_fits(sorted(lines)):
        raise SchemaTooLarge("more characters than MAX_SCHEMA_CHARS")
    return sorted(lines)


def schema_object_name(line: str) -> str:
    """An inventory line without its digest: what the object is, whatever its definition."""
    return line.rsplit(" #", 1)[0]


def schema_changes(baseline: list[str], candidate: list[str]) -> list[str]:
    """What the candidate's inventory has that the baseline's doesn't, as `added: ...` or
    `changed: ...` lines without digests. Changed means the same object with another definition. An
    object the candidate dropped is not listed: it can't stand between the check and the tables."""
    before, known = set(baseline), {schema_object_name(line) for line in baseline}
    return [
        f"{'changed' if schema_object_name(line) in known else 'added'}: {schema_object_name(line)}"
        for line in sorted(set(candidate) - before)
    ]


def retire_owner_sql(owner: str) -> str:
    """owner is pattern-checked by PrestartConfig, so it is a plain identifier."""
    return (
        f"ALTER ROLE {owner} NOLOGIN; "
        f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = '{owner}' AND pid <> pg_backend_pid();"
    )


def prestart_tail(log: str, passwords: tuple[str, ...] = ()) -> str:
    """The end of a prestart's output on one line, marked as the project's text. The log was cleaned
    by the sandbox; this run's database passwords are taken out, since the command held two of them."""
    secrets_lower = [password.lower() for password in passwords if password]
    if secrets_lower:
        # Any run of 8 or more hex characters that is part of a password: the log tail may cut one,
        # and a tool may print it in another case.
        log = re.sub(
            r"[0-9A-Fa-f]{8,}",
            lambda found: "[password]" if any(found.group(0).lower() in secret for secret in secrets_lower) else found.group(0),
            log,
        )
    return f"{PROJECT} {' '.join(log.split())[-PRESTART_LOG_CHARS:]}"


@dataclass
class StackSession(_Session):
    instance_id: str = ""
    hooks_token: str = ""
    namespace: str = ""
    prestart_facts: ContainerFacts | None = None
    # The inventory a prestart stack had before anything of the project's ran as the hooks role.
    schema_start: list[str] | None = None

    @property
    def postgres_name(self) -> str:
        return f"ff-{self.sandbox_id}-postgres"

    @property
    def redis_name(self) -> str:
        return f"ff-{self.sandbox_id}-redis"

    @property
    def hooks_name(self) -> str:
        return f"ff-{self.sandbox_id}-hooks"

    @property
    def prestart_name(self) -> str:
        return f"ff-{self.sandbox_id}-prestart"

    @property
    def helper_check_name(self) -> str:
        return f"ff-{self.sandbox_id}-helpercheck"

    def helper_check_argv(self, expires_at: int) -> list[str]:
        """A container of the helper's image that only compares the helper's file with this
        FaultForge's and exits: sealed like the helper, with no network at all, and removed as it
        ends. It runs before anything of the project's does."""
        sealed = _common_args(self.helper_check_name, self.sandbox_id, "helpercheck", HOOKS_LIMITS, expires_at)
        return [
            *("--rm" if item == "--detach" else item for item in sealed), "--network", "none", "--user", HOOKS_USER,
            HOOKS_IMAGE, "python", "-I", "-c", _HELPER_IS_CURRENT, helper_sha256(),
        ]

    def _require_current_helper(self, expires_at: int) -> None:
        """An older helper would run the pinned SQL without row_security = off, or as another role."""
        checked = _docker(self.policy, self.helper_check_argv(expires_at), STATE_EXEC_TIMEOUT_S)
        if checked.returncode == 0:
            return
        if checked.returncode == HELPER_DIFFERS:
            detail = f"{HOOKS_IMAGE} holds another version of the hooks helper"
        elif checked.returncode == HELPER_UNREADABLE:
            detail = f"{HOOKS_IMAGE} has no hooks helper that can be read at /opt/stack_hooks.py"
        else:
            detail = f"the hooks helper in {HOOKS_IMAGE} couldn't be checked (exit status {checked.returncode}): {_stderr(checked)}"
        raise SandboxError("image_missing", f"{detail}; rebuild it with scripts/build_images.sh")

    def _start(self, args: list[str], failure: str = "target_start_failed") -> None:
        self._within_wall(args, failure)  # type: ignore[arg-type]

    def _image_id(self, image: str) -> str:
        result = _docker(self.policy, ["image", "inspect", "--format", "{{.Id}}", image])
        if result.returncode != 0:
            raise SandboxError("image_missing", f"{image} isn't built")
        return result.stdout.decode("ascii", "replace").strip()

    def argv(
        self, password: str, expires_at: int, admin_password: str = "", owner_password: str = "", hooks_password: str = "",
    ) -> dict[str, list[str]]:
        stack = self.spec.stack
        namespace = ["--network", f"container:{self.postgres_name}"]
        database_url = f"postgresql://{stack.database_user}:{password}@127.0.0.1:5432/{stack.database_name}"
        redis_env = {"REDIS_URL": "redis://127.0.0.1:6379"} if stack.redis_image is not None else {}
        postgres = [
            *_common_args(self.postgres_name, self.sandbox_id, "postgres", POSTGRES_LIMITS, expires_at),
            "--network", "none", "--user", POSTGRES_USER,
            "--tmpfs", "/var/lib/postgresql/data:rw,uid=70,gid=70,mode=0700,size=256m",
            "--tmpfs", "/var/run/postgresql:rw,uid=70,gid=70,size=1m",
            *_env_args({
                "POSTGRES_PASSWORD": admin_password, "POSTGRES_USER": ADMIN_ROLE, "POSTGRES_DB": stack.database_name,
                "POSTGRES_INITDB_ARGS": POSTGRES_INITDB_ARGS,
            }),
            stack.postgres_image,
        ]
        uid = stack.app_user.split(":")[0]
        writable = [argument for path in stack.app_writable for argument in ("--tmpfs", f"{path}:rw,uid={uid},size=64m")]
        app = [
            *_common_args(self.target_name, self.sandbox_id, "target", self.policy.target, expires_at),
            *namespace, "--user", stack.app_user, *writable,
            *_env_args({
                **stack.env, "POSTGRES_HOST": "127.0.0.1", "POSTGRES_USER": stack.database_user, "DATABASE_URL": database_url,
                **redis_env, "HOME": "/tmp", "PORT": str(stack.app_port), "HOSTNAME": "127.0.0.1",
            }),
            self.spec.app_image,
        ]
        # The helper logs in as the hooks role, over TCP with that role's own password. It never
        # holds the administrator's.
        hooks_spec = {
            "port": stack.hooks_port, "database_user": HOOKS_ROLE, "database_name": stack.database_name,
            "app_health_url": f"http://127.0.0.1:{stack.app_port}{stack.app_health_path}",
            "reset_sql": stack.reset_sql, "state_sql": dict(stack.state_sql),
            "build_id": self.deployed_code_sha256, "instance_id": self.instance_id,
            "started_at": datetime.fromtimestamp(self.started_wall, UTC).isoformat(),
            "search_path": ADMIN_SEARCH_PATH,
        }
        hooks = [
            *_common_args(self.hooks_name, self.sandbox_id, "hooks", HOOKS_LIMITS, expires_at),
            *namespace, "--user", HOOKS_USER,
            *_env_args({
                "FF_STACK_HOOKS": json.dumps(hooks_spec), "FF_DATABASE_PASSWORD": hooks_password,
                HOOKS_TOKEN_ENV: self.hooks_token, "PYTHONDONTWRITEBYTECODE": "1",
            }),
            HOOKS_IMAGE, "python", "-I", "/opt/stack_hooks.py",
        ]
        built = {"postgres": postgres, "app": app, "hooks": hooks}
        step = stack.prestart
        if step is not None:
            # The app's image, user, limits and namespace, and the app's settings. The differences are
            # the command and the owner's URL, which the app never gets. --entrypoint, so an image's
            # own entrypoint can't stand between the sandbox and the command stack.yaml names.
            owner_url = f"postgresql://{step.owner_role}:{owner_password}@127.0.0.1:5432/{stack.database_name}"
            built["prestart"] = [
                *_common_args(self.prestart_name, self.sandbox_id, PRESTART_ROLE, self.policy.target, expires_at),
                *namespace, "--user", stack.app_user, *writable,
                *_env_args({**stack.env, "DATABASE_URL": database_url, step.owner_url_env: owner_url, **redis_env, "HOME": "/tmp"}),
                "--entrypoint", step.command[0], self.spec.app_image, *step.command[1:],
            ]
        if stack.redis_image is not None:
            built["redis"] = [
                *_common_args(self.redis_name, self.sandbox_id, "redis", REDIS_LIMITS, expires_at),
                *namespace, "--user", REDIS_USER, "--tmpfs", "/data:rw,uid=999,size=4m",
                stack.redis_image, "redis-server", "--save", "", "--appendonly", "no", "--bind", "127.0.0.1",
            ]
        return built

    def execute(self) -> Report:
        stack = self.spec.stack
        expires_at = expiry_unix(self.started_wall, self.policy)
        _require_valid_verifier(self.spec.state_verifier)
        try:
            _prepare_project(self.spec, self.project_dir, self.scenario_id)
        except Exception as error:
            if isinstance(error, SandboxError):
                raise
            raise SandboxError("invalid_spec", type(error).__name__) from error
        self._require_helper_origin()
        reap_stale(self.policy)
        for image in (stack.postgres_image, stack.redis_image, HOOKS_IMAGE, self.policy.runner_image):
            if image is not None:
                self._image_id(image)
        # What the host knows ran: the image's own ID, which the app can't change, or for an image
        # FaultForge built from source, the hash of that source.
        self.deployed_code_sha256 = self.spec.deployed_code(self._image_id(self.spec.app_image))
        self.instance_id = secrets.token_hex(8)
        self.hooks_token = secrets.token_hex(16)
        password = secrets.token_hex(16)
        owner_password = secrets.token_hex(16) if stack.prestart is not None else ""
        hooks_password = secrets.token_hex(16)
        argv = self.argv(password, expires_at, secrets.token_hex(16), owner_password, hooks_password)
        runner_args = runner_argv(
            self.runner_name, self.sandbox_id, self.postgres_name, self.project_dir, self.scenario_id,
            self.options, self.policy, expires_at, extra_env={HOOKS_TOKEN_ENV: self.hooks_token},
        )
        self.containers_started = True
        # First of all: before postgres, the prestart or the app, so nothing of the project's runs
        # beside a helper that isn't this FaultForge's.
        self._require_current_helper(expires_at)
        self._start(argv["postgres"])
        self._await(["exec", self.postgres_name, "pg_isready", "-h", "127.0.0.1", "-U", ADMIN_ROLE], "postgres")
        version = admin_psql_argv(self.postgres_name, stack, "SHOW server_version_num")
        version[version.index("-c"):version.index("-c")] = ["-tA"]
        shown = _docker(self.policy, version, STATE_EXEC_TIMEOUT_S)
        problem = postgres_version_problem(shown.stdout or b"") if shown.returncode == 0 else "the database server's version couldn't be read"
        if problem is not None:
            raise SandboxError("invalid_spec", problem)
        setup = _docker(
            self.policy, admin_psql_argv(self.postgres_name, stack, setup_role_sql(stack, password, owner_password, hooks_password=hooks_password)),
            STATE_EXEC_TIMEOUT_S,
        )
        if setup.returncode != 0:
            raise SandboxError("target_start_failed", "the database role couldn't be set up")
        if "redis" in argv:
            self._start(argv["redis"])
        if "prestart" in argv:
            # Before the helper: it never sees the helper's token, and it is gone before either starts.
            self._run_prestart(argv["prestart"], (password, owner_password))
            self._retire_owner(stack)
            # The migrations are in, and nothing of the project's has run as the hooks role yet.
            self.schema_start = self._schema_inventory()
        # Before the app, so that the app can't bind the helper's port first.
        self._start(argv["hooks"])
        self._await(["exec", self.hooks_name, "python", "-I", "-c", _PORT_BOUND.format(port=stack.hooks_port)], "hooks")
        demanded = _docker(
            self.policy, ["exec", self.hooks_name, "python", "-I", "-c", _TOKEN_DEMANDED.format(port=stack.hooks_port)],
            STATE_EXEC_TIMEOUT_S,
        )
        if demanded.returncode != 0:
            raise SandboxError(
                "image_missing", f"{HOOKS_IMAGE} doesn't demand the run's token; rebuild it with scripts/build_images.sh",
            )
        self._start(argv["app"])
        try:
            self._within_wall(runner_args, "runner_start_failed")
        except SandboxError as error:
            if error.reason == "runner_start_failed" and not self._target_is_running():
                raise SandboxError("target_exited", error.detail) from error
            raise
        return self._await_runner()

    def _retire_owner(self, stack: StackConfig) -> None:
        """The owner is for the prestart only. A migration could set its password, or leave a session
        open, so once the prestart is gone the owner can't log in and any session it left is ended."""
        owner = stack.prestart.owner_role if stack.prestart is not None else None
        assert owner is not None
        retired = _docker(self.policy, admin_psql_argv(self.postgres_name, stack, retire_owner_sql(owner)), STATE_EXEC_TIMEOUT_S)
        if retired.returncode != 0:
            raise SandboxError("target_start_failed", "the database owner couldn't be locked after the prestart")

    def _read_database(self) -> None:
        """The state first, straight after the runner, then the inventory, for every stack."""
        super()._read_database()
        self._finish_schema_inventory()

    def _finish_schema_inventory(self) -> None:
        """The read at the end of the run, and what the run records. Doesn't raise.

        Without a prestart this is the only read: the app makes its own schema as it starts, and
        only the runner knows when it has. With one, the first read came before any of the project's
        code ran as the hooks role, and a check constraint, a default or a trigger can run such code
        during a reset. So what is recorded is both reads together, and what this one has that the
        first hadn't is kept apart: it appeared during the run. If either read failed, nothing is
        recorded and the reason stands."""
        two = self.spec.stack.prestart is not None
        if two and self.schema_start is None:
            self.schema_failure = self.schema_failure or "not_read"
            return
        end = self._schema_inventory()
        if end is None:
            return
        both = sorted({*(self.schema_start or []), *end})
        if not schema_fits(both):
            self.schema_failure = SCHEMA_TOO_MANY
            return
        self.schema_objects = both
        self.schema_during = sorted(set(end) - set(self.schema_start or [])) if two else []

    def _schema_inventory(self) -> list[str] | None:
        """One read of the catalog. Doesn't raise: a gap is None with its reason in schema_failure,
        and a fix compared with this run is then flagged for review. It never fails a run and never
        changes a verdict."""
        try:
            timeout_s = min(self.policy.docker_timeout_s, STATE_EXEC_TIMEOUT_S)
            with tempfile.TemporaryFile() as out:
                result = _docker(
                    self.policy, schema_inventory_argv(self.postgres_name, self.spec.stack), timeout_s,
                    stdout=out, stderr=subprocess.DEVNULL,
                )
                out.seek(0)
                output = out.read(MAX_SCHEMA_OUTPUT_BYTES + 1)
            if result.returncode != 0:
                self.schema_failure = f"exec_exited_{result.returncode}"
                return None
            return parse_schema_inventory(output)
        except _DockerTimeout:
            self.schema_failure = "exec_timed_out"
        except SandboxError:
            self.schema_failure = "docker_unavailable"
        except SchemaTooLarge:
            self.schema_failure = SCHEMA_TOO_MANY
        except ValueError:
            self.schema_failure = "invalid_output"
        except Exception as error:
            self.schema_failure = f"internal_error_{type(error).__name__}"
        return None

    def _run_prestart(self, args: list[str], passwords: tuple[str, ...]) -> None:
        """Runs the prestart command to its end and removes its container, by its exact name. Only
        an exit status of 0 inside its limit lets the stack go on: anything else is
        target_start_failed, with the end of the command's output marked as the project's text."""
        step = self.spec.stack.prestart
        assert step is not None
        name = self.prestart_name
        self._start(args)
        status: int | None = None
        try:
            try:
                waited = _docker(self.policy, ["wait", name], min(step.timeout_s, self._remaining_s()))
            except _DockerTimeout:
                _docker(self.policy, ["kill", name])
                waited = None
            if waited is not None:
                try:
                    status = int(waited.stdout.strip())
                except ValueError as error:
                    raise SandboxError("docker_unavailable", f"docker wait failed: {_stderr(waited)}") from error
            _, tail = self._read_logs(name, 0)
            postgres = _docker(self.policy, ["inspect", "--format", "{{.Id}}", self.postgres_name])
            joined = postgres.stdout.decode("ascii", "replace").strip() if postgres.returncode == 0 else ""
            self.prestart_facts = self._inspect(name, self.policy.target, f"container:{joined or 'unknown'}", self.spec.stack.app_user)
        finally:
            _docker(self.policy, ["rm", "--force", "--volumes", name])
        if status is None:
            if self._remaining_s() <= 0:
                raise SandboxError("wall_limit_exceeded", "the prestart step ran past the wall limit")
            raise SandboxError("target_start_failed", f"the prestart step passed its {step.timeout_s:g} s limit and was killed")
        if status != 0:
            raise SandboxError("target_start_failed", f"the prestart step exited with status {status}. Its output: {prestart_tail(tail, passwords)}")

    def _require_helper_origin(self) -> None:
        """The runner sends this run's hooks token to target.hooks_origin, so that must be the
        helper's own port and never the app's."""
        config = config_module.parse_config(Path(self.spec.config_path).read_text(encoding="utf-8"))
        expected = f"http://127.0.0.1:{self.spec.stack.hooks_port}"
        if config.target.hooks_origin != expected:
            raise SandboxError("invalid_spec", f"a stack's target.hooks_origin must be {expected}, where the hooks helper listens")

    def _await(self, args: list[str], name: str) -> None:
        deadline = time.monotonic() + READY_WAIT_S
        while time.monotonic() < deadline:
            if _docker(self.policy, args, STATE_EXEC_TIMEOUT_S).returncode == 0:
                return
            time.sleep(READY_POLL_S)
        raise SandboxError("target_start_failed", f"{name} didn't become ready")

    def state_verification(self, report: Report | None) -> StateVerification | None:
        """A run where a trusted service died can't pass, because the hooks' answers mean nothing."""
        names = {POSTGRES_USER, HOOKS_USER}
        trusted = [facts for facts in self.service_facts if facts.user in names and facts is not self.prestart_facts]
        if len(trusted) != len(names) or not all(facts.running_when_inspected for facts in trusted):
            return StateVerification(status="unavailable", reason="trusted_service_not_running")
        return super().state_verification(report)

    def _query_database(self, queries: Mapping[str, str]) -> dict[str, int] | None:
        timeout_s = min(self.policy.docker_timeout_s, STATE_EXEC_TIMEOUT_S)
        with tempfile.TemporaryFile() as out:
            result = _docker(
                self.policy, psql_state_argv(self.postgres_name, self.spec.stack, queries), timeout_s,
                stdout=out, stderr=subprocess.DEVNULL,
            )
            out.seek(0)
            output = out.read(MAX_STATE_OUTPUT_BYTES + 1)
        if result.returncode != 0:
            self.database_failure = f"exec_exited_{result.returncode}"
            return None
        return parse_state_output(output, queries)

    def _collect_target(self) -> None:
        self.target_stdout, self.target_log_tail = self._read_logs(self.target_name, 1024 * 1024)
        postgres = self._inspect(self.postgres_name, POSTGRES_LIMITS, "none", POSTGRES_USER)
        if postgres is not None:
            self.service_facts.append(postgres)
        joined = f"container:{postgres.container_id}" if postgres is not None else ""
        self.namespace = joined
        self.target_facts = self._inspect(self.target_name, self.policy.target, joined, self.spec.stack.app_user)
        others = [(self.hooks_name, HOOKS_LIMITS, HOOKS_USER)]
        if self.spec.stack.redis_image is not None:
            others.insert(0, (self.redis_name, REDIS_LIMITS, REDIS_USER))
        for name, limits, user in others:
            facts = self._inspect(name, limits, joined, user)
            if facts is not None:
                self.service_facts.append(facts)
        # Read before it was removed. It isn't a trusted service: state_verification leaves it out.
        if self.prestart_facts is not None:
            self.service_facts.append(self.prestart_facts)


    def _collect_runner(self) -> None:
        """A stack's runner joins postgres's namespace, not the app's, so that is what its facts are
        checked against."""
        self.runner_stdout, _ = self._read_logs(self.runner_name, MAX_RUNNER_STDOUT_BYTES)
        self.runner_facts = self._inspect(self.runner_name, self.policy.runner, self.namespace or "unknown", f"{os.getuid()}:{os.getgid()}")


def run_stack(
    spec: StackSpec, project_dir: Path, scenario_id: str, options: RunOptions = RunOptions(),
    policy: SandboxPolicy = STACK_POLICY,
) -> SandboxResult:
    """Returns a result for every failure, rather than raising. A failed result has no report."""
    return run_session(StackSession(spec, Path(project_dir).resolve(), scenario_id, options, policy, time.monotonic()))
