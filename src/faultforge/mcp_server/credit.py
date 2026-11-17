"""The credit floor: what the MCP server may spend, and when it must not (docs/PLAN.md V4.3).

A paid operation is allowed only if `credit - worst_case_cost(op) >= $10`, where credit is
`-balance - pending_charges` from Vultr's `GET /v2/account`. Everything here fails closed: no key, a
key file that isn't private, an HTTP error, a timeout, a reply that can't be parsed, a number that
isn't finite, or a reading older than ten minutes all count as below the floor, each with a short
fixed reason.

The floor is a buffer, not a hard limit. Vultr's figure can lag FaultForge's own spend, and a model
reply can report more usage than the per-call cap was checked against, so the credit can dip a
little under $10 and one fix can cost more than its worst case. The ledger's caps are the outer bound.

A second bound is local. FaultForge's own inference spend goes into a ledger under the state
directory (web/budget.py's BudgetLedger, with the web app's $2 a day and $15 total caps). Spend
recorded after the API reading was taken is subtracted from it, so a reading made just before
spending stays honest.

The billing key may be the account's main key, with every permission. So it is only ever sent with
one request: GET https://api.vultr.com/v2/account, with no query and no body, and redirects are
never followed. AccountOnlyTransport sits under the one client that carries the key and refuses
anything else before it leaves, whatever the code above it asked for.

Neither key ever leaves this module except as an ApiKey, whose repr hides it. No result, log line or
exception text holds a key, a response body or an exception's own text.
"""

import json
import math
import os
import stat
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal, InvalidOperation
from pathlib import Path

import httpx

from faultforge import fix_agent
from faultforge.fix_agent import ApiKey
from faultforge.web.budget import BudgetLedger, LedgerBudget
from faultforge.web.settings import AGENT_MODEL, AGENT_PRICES, KEY_NAME as INFERENCE_KEY_NAME, WebSettings

ACCOUNT_URL = "https://api.vultr.com/v2/account"
BILLING_KEY_NAME = "VULTR_BILLING_API_KEY"
BILLING_ENV = "FF_VULTR_ENV"
INFERENCE_ENV = "FF_INFERENCE_ENV"
LEDGER_NAME = "spend.json"
FLOOR_USD = Decimal("10.00")
MAX_READING_AGE_S = 600.0
ACCOUNT_TIMEOUT = httpx.Timeout(5.0)
ACCOUNT_DEADLINE_S = 10.0
MAX_ACCOUNT_BYTES = 64 * 1024
MAX_KEY_FILE_BYTES = 4096
MAX_SANE_USD = Decimal("1000000000")
# The most one model call of a vultr fix may cost, checked before the call is sent. fix_agent asks
# the budget about the worst case of each call (the prompt's upper bound plus max_tokens of output),
# and CappedLedgerBudget refuses any call whose worst case is above this. So the worst case of a
# whole fix is this times the attempts, whatever the prompt grows to.
MAX_CALL_USD = 0.10
FIX_ATTEMPTS = fix_agent.MAX_ATTEMPTS
FIX_MAX_TOKENS = WebSettings.agent_max_tokens

NO_BILLING_KEY = "no billing key"
BAD_KEY_FILE = "the key file isn't a private regular file (mode 0600, owned by this user, not a link)"
NO_KEY_LINE = "the key file has no usable key line"
STALE = "the credit reading is older than 10 minutes"
NEVER_READ = "the credit hasn't been read yet"
CHECK_FAILED = "the credit check failed"


class RefusedRequest(Exception):
    """A request other than the one account read. The message is fixed and quotes nothing."""

    def __init__(self) -> None:
        super().__init__("only GET https://api.vultr.com/v2/account may carry the billing key")


def require_account_request(request: httpx.Request) -> None:
    """Raises unless the request is exactly the account read: the method, scheme, host, port, path,
    query, fragment, userinfo and body are all checked, on the request as it would be sent."""
    url = request.url
    allowed = (
        request.method == "GET" and url.scheme == "https" and url.host == "api.vultr.com" and url.port is None
        and url.raw_path == b"/v2/account" and not url.query and not url.fragment and not url.userinfo
        and request.headers.get("content-length", "0") == "0" and "transfer-encoding" not in request.headers
    )
    if allowed:
        try:
            allowed = request.read() == b""
        except Exception:
            allowed = False
    if not allowed:
        raise RefusedRequest


class AccountOnlyTransport(httpx.BaseTransport):
    """The only transport the billing key is ever sent through. It checks every request that
    reaches it, a redirect's included, so no caller can send the key anywhere else by mistake."""

    def __init__(self, inner: httpx.BaseTransport):
        self._inner = inner

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        require_account_request(request)
        return self._inner.handle_request(request)

    def close(self) -> None:
        self._inner.close()


def billing_client(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    """The one client that may carry the billing key. Redirects are off, the environment's proxies
    are ignored, and whatever is sent goes through AccountOnlyTransport."""
    inner = transport if transport is not None else httpx.HTTPTransport(trust_env=False)
    return httpx.Client(transport=AccountOnlyTransport(inner), trust_env=False, follow_redirects=False, timeout=ACCOUNT_TIMEOUT)


def billing_key_path() -> Path:
    configured = os.environ.get(BILLING_ENV)
    return Path(configured) if configured else Path.home() / ".config" / "faultforge" / "vultr.env"


def inference_key_path() -> Path:
    configured = os.environ.get(INFERENCE_ENV)
    return Path(configured) if configured else Path.home() / ".config" / "faultforge" / "inference.env"


def read_key_file(path: Path, name: str) -> tuple[ApiKey | None, str | None]:
    """(key, None) or (None, a fixed reason). The file must be a regular file that this user owns and
    nobody else can read, and it is checked through the descriptor that is then read, so it can't be
    swapped in between. No reason ever quotes the file."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except FileNotFoundError:
        return None, "no key file"
    except OSError:
        return None, BAD_KEY_FILE
    try:
        info = os.fstat(descriptor)
        private = stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) & 0o077 == 0 and info.st_uid == os.getuid()
        if not private or info.st_size > MAX_KEY_FILE_BYTES:
            return None, BAD_KEY_FILE
        data = os.read(descriptor, MAX_KEY_FILE_BYTES + 1)
    except OSError:
        return None, BAD_KEY_FILE
    finally:
        os.close(descriptor)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None, NO_KEY_LINE
    for line in text.splitlines():
        key, separator, value = line.strip().removeprefix("export ").partition("=")
        if separator and key.strip() == name:
            try:
                return ApiKey(value.strip().strip("'\"")), None
            except ValueError:
                return None, NO_KEY_LINE
    return None, NO_KEY_LINE


def billing_key() -> tuple[ApiKey | None, str | None]:
    key, reason = read_key_file(billing_key_path(), BILLING_KEY_NAME)
    return key, (NO_BILLING_KEY if reason == "no key file" else f"billing key: {reason}") if reason else None


def inference_key() -> tuple[ApiKey | None, str | None]:
    key, reason = read_key_file(inference_key_path(), INFERENCE_KEY_NAME)
    return key, ("no inference key" if reason == "no key file" else f"inference key: {reason}") if reason else None


def usd(value: float | Decimal) -> Decimal:
    """A float's shortest decimal form, so 0.3 is 0.3 and the floor is compared exactly."""
    return value if isinstance(value, Decimal) else Decimal(repr(value))


def money(value: Decimal) -> str:
    """Whole cents, rounded down, so a credit just under an amount never reads as that amount."""
    return f"${value.quantize(Decimal('0.01'), rounding=ROUND_FLOOR)}"


def fix_worst_case_usd(attempts: int = FIX_ATTEMPTS) -> Decimal:
    """The most a vultr fix can cost: the per-call cap for every attempt."""
    return usd(MAX_CALL_USD) * attempts


def remote_run_worst_case_usd(hourly_rate_usd: float, wall_seconds: float) -> Decimal:
    """For the remote runtime (M12): the VM's hourly rate for the run's wall limit, rounded up to
    whole hours and never less than one. Used by the remote runtime (mcp_server/remote.py)."""
    if not (math.isfinite(hourly_rate_usd) and hourly_rate_usd > 0 and math.isfinite(wall_seconds) and wall_seconds >= 0):
        raise ValueError("the hourly rate must be positive and the wall limit non-negative")
    return usd(hourly_rate_usd) * max(1, math.ceil(wall_seconds / 3600))


class CappedLedgerBudget(LedgerBudget):
    """The web app's ledger-backed budget, with the per-call cap on top. Nothing is loosened: a call
    must pass both."""

    def can_afford(self, model: str, input_tokens: int, max_tokens: int) -> bool:
        return self.cost(model, input_tokens, max_tokens) <= MAX_CALL_USD and super().can_afford(model, input_tokens, max_tokens)


@dataclass(frozen=True)
class Reading:
    credit_usd: Decimal
    # A monotonic time, not the wall clock.
    at: float
    # The ledger's total when the reading was taken. What the ledger gained since is spend the
    # reading can't have seen.
    ledger_total_usd: Decimal


@dataclass(frozen=True)
class CreditView:
    """What a result may show. credit_usd is the reading less the ledger's spend since, or None."""

    credit_usd: Decimal | None
    age_s: float | None
    reason: str | None
    ledger: dict[str, float | int] | None
    ledger_reason: str | None

    def rounded(self) -> int | None:
        return None if self.credit_usd is None else int(self.credit_usd.to_integral_value(rounding="ROUND_FLOOR"))


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str
    view: CreditView


def _refuse_constant(name: str) -> None:
    raise ValueError(f"{name} isn't JSON")


def _amount(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise ValueError("not a number")
    amount = Decimal(value)
    if not amount.is_finite() or abs(amount) > MAX_SANE_USD:
        raise ValueError("not a finite amount")
    return amount


def parse_account(body: bytes) -> Decimal:
    """Credit from the account reply, or ValueError. Decimals, so cents stay exact."""
    try:
        payload = json.loads(body, parse_float=Decimal, parse_constant=_refuse_constant)
        account = payload["account"]
        pending = _amount(account["pending_charges"])
        if pending < 0:
            # Charges can't be negative. A reply that says so would raise the credit, so it isn't believed.
            raise ValueError("negative pending charges")
        return -_amount(account["balance"]) - pending
    except (ValueError, KeyError, TypeError, RecursionError, InvalidOperation, ArithmeticError):
        raise ValueError("unparseable account reply") from None


class CreditGuard:
    """One per server. The reading is cached in memory with its time; the ledger is read from disk
    on every question, so two servers sharing a state directory see each other's spend."""

    def __init__(
        self, *, transport: httpx.BaseTransport | None = None, clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self._transport = transport
        self._clock = clock
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._reading: Reading | None = None
        self._failure: str | None = None

    def __repr__(self) -> str:
        return "CreditGuard()"

    def ledger(self, state: Path) -> BudgetLedger:
        return BudgetLedger(
            state / LEDGER_NAME, daily_usd_cap=WebSettings.daily_usd_cap, total_usd_cap=WebSettings.total_usd_cap,
            daily_token_cap=WebSettings.daily_token_cap, clock=self._clock,
        )

    def budget(self, ledger: BudgetLedger) -> CappedLedgerBudget:
        return CappedLedgerBudget(ledger, AGENT_PRICES)

    def _fetch(self, key: ApiKey) -> tuple[Decimal | None, str | None]:
        """One GET, through billing_client and nothing else. Every failure is a fixed reason; the
        exception and the body are dropped. Any status but 200 fails, so a redirect is never followed."""
        deadline = self._monotonic() + ACCOUNT_DEADLINE_S
        headers = {"Authorization": f"Bearer {key.reveal()}", "Accept": "application/json", "Accept-Encoding": "identity"}
        try:
            with billing_client(self._transport) as client:
                with client.stream("GET", ACCOUNT_URL, headers=headers) as response:
                    if response.status_code != 200:
                        return None, f"{CHECK_FAILED}: HTTP {int(response.status_code)}"
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        body += chunk
                        if len(body) > MAX_ACCOUNT_BYTES:
                            return None, f"{CHECK_FAILED}: unparseable reply"
                        if self._monotonic() > deadline:
                            return None, f"{CHECK_FAILED}: timeout"
        except httpx.TimeoutException:
            return None, f"{CHECK_FAILED}: timeout"
        except httpx.HTTPError:
            return None, f"{CHECK_FAILED}: network error"
        except Exception:
            return None, f"{CHECK_FAILED}: internal error"
        try:
            return parse_account(bytes(body)), None
        except ValueError:
            return None, f"{CHECK_FAILED}: unparseable reply"

    def _fresh(self, now: float) -> Reading | None:
        reading = self._reading
        # now and reading.at are monotonic times, so a wall clock that is set back can't keep a
        # reading fresh. The wall clock is only for the ledger's days.
        return reading if reading is not None and 0 <= now - reading.at <= MAX_READING_AGE_S else None

    def view(self, state: Path, *, refresh: bool) -> CreditView:
        """The credit as it can be shown. With refresh, a missing or stale reading is read again from
        the API; without, nothing touches the network and a stale reading has no credit to show."""
        with self._lock:
            ledger = self.ledger(state)
            ledger_reason = ledger.blocked_reason()
            snapshot = None if ledger_reason == "ledger_unavailable" else ledger.snapshot()
            now = self._monotonic()
            reading = self._fresh(now)
            if reading is None and refresh:
                reading = self._refresh(snapshot, now)
            if snapshot is None:
                return CreditView(None, None, "the spend ledger can't be read", None, ledger_reason)
            if reading is None:
                stale = self._reading is not None
                age = round(now - self._reading.at, 1) if stale else None
                reason = self._failure or (STALE if stale else NEVER_READ)
                return CreditView(None, age, reason, snapshot, ledger_reason)
            since = max(Decimal(0), usd(snapshot["total_usd_spent"]) - reading.ledger_total_usd)
            return CreditView(reading.credit_usd - since, round(now - reading.at, 1), None, snapshot, ledger_reason)

    def _refresh(self, snapshot: dict[str, float | int] | None, now: float) -> Reading | None:
        self._failure = None
        if snapshot is None:
            return None
        key, reason = billing_key()
        if key is None:
            self._reading, self._failure = None, reason
            return None
        credit, reason = self._fetch(key)
        if credit is None:
            self._reading, self._failure = None, reason
            return None
        self._reading = Reading(credit, now, usd(snapshot["total_usd_spent"]))
        return self._reading

    def decide(self, state: Path, worst_case_usd: Decimal, *, refresh: bool = True) -> Decision:
        """Whether a paid operation that can cost up to worst_case_usd may start. The ledger's caps
        are asked first, so a capped ledger never causes a network call. Without refresh nothing
        touches the network: the answer is what the cached reading supports, which is a refusal
        when there is none. Only an answer made with refresh may start a paid operation."""
        ledger = self.ledger(state)
        blocked = ledger.blocked_reason(float(worst_case_usd))
        if blocked is not None:
            return Decision(False, f"the spend ledger refuses it ({blocked})", self.view(state, refresh=False))
        view = self.view(state, refresh=refresh)
        if view.credit_usd is None:
            return Decision(False, view.reason or CHECK_FAILED, view)
        if view.credit_usd - worst_case_usd < FLOOR_USD:
            return Decision(
                False,
                f"credit {money(view.credit_usd)} is below the {money(FLOOR_USD)} floor after the worst case ({money(worst_case_usd)})",
                view,
            )
        return Decision(True, f"credit {money(view.credit_usd)} stays at or above the {money(FLOOR_USD)} floor after the worst case ({money(worst_case_usd)})", view)
