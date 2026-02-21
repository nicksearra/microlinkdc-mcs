"""
MCS Stream B — Authentication & Multi-Tenant Middleware
========================================================
JWT-based authentication, per-tenant rate limiting, row-level security
enforcement, and request audit logging for the MicroLink Control System.

Integration:
    from auth import auth_router, AuthMiddleware, require_roles
    app.include_router(auth_router)
    app.add_middleware(AuthMiddleware)

    # Protect an endpoint:
    @app.get("/admin-only", dependencies=[Depends(require_roles("admin"))])
    async def admin_endpoint(request: Request): ...
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import time
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Generator, Optional
from uuid import UUID

import bcrypt
import jwt
import psycopg2
import psycopg2.extras
import psycopg2.pool
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    Response,
    status,
)
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

# =============================================================================
# CONFIGURATION
# =============================================================================

JWT_SECRET = os.getenv("JWT_SECRET", "CHANGE-ME-in-production-use-256-bit-random")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
JWT_EXPIRY_MINUTES = int(os.getenv("JWT_EXPIRY_MINUTES", "60"))
JWT_REFRESH_EXPIRY_HOURS = int(os.getenv("JWT_REFRESH_EXPIRY_HOURS", "24"))

# Paths that don't require authentication
PUBLIC_PATHS = {
    "/health",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/auth/token",
}

# =============================================================================
# LOGGING
# =============================================================================

log = logging.getLogger("mcs.auth")

# =============================================================================
# DATABASE
# =============================================================================

DB_DSN = (
    f"host={os.getenv('DB_HOST', 'localhost')} "
    f"port={os.getenv('DB_PORT', '5432')} "
    f"dbname={os.getenv('DB_NAME', 'mcs')} "
    f"user={os.getenv('DB_USER', 'mcs')} "
    f"password={os.getenv('DB_PASSWORD', '')}"
)

_pool: Optional[psycopg2.pool.ThreadedConnectionPool] = None


def get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _pool
    if _pool is None or _pool.closed:
        _pool = psycopg2.pool.ThreadedConnectionPool(2, 10, dsn=DB_DSN)
    return _pool


@contextmanager
def get_db() -> Generator[psycopg2.extensions.connection, None, None]:
    pool = get_pool()
    conn = pool.getconn()
    try:
        yield conn
    finally:
        pool.putconn(conn)


def db_dep() -> Generator[psycopg2.extensions.connection, None, None]:
    with get_db() as conn:
        yield conn


# =============================================================================
# VALID ROLES
# =============================================================================

VALID_ROLES = {"admin", "operator", "viewer", "customer", "host", "lender"}

# Tier → default rate limit (requests per minute)
TIER_RATE_LIMITS = {
    "internal": 1000,
    "customer": 100,
    "host": 100,
    "lender": 50,
}

# Tier → data scope restrictions
# Defines which subsystem prefixes each tier can access
TIER_DATA_SCOPES = {
    "internal": None,        # Full access — no filtering
    "customer": None,        # Filtered by block assignment, not subsystem
    "host": [                # Thermal + ESG data only
        "thermal-l3", "thermal-hx", "thermal-reject",
        "environmental",
    ],
    "lender": [],            # No direct telemetry — only summary/billing endpoints
}


# =============================================================================
# PYDANTIC MODELS
# =============================================================================

class TokenRequest(BaseModel):
    api_key: str = Field(..., min_length=32, description="API key issued at tenant creation")


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    tenant_id: str
    tenant_name: str
    tier: str
    roles: list[str]


class RefreshRequest(BaseModel):
    refresh_token: str = Field(..., description="Refresh token from /auth/token response")


class RefreshResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class APIKeyCreateRequest(BaseModel):
    tenant_id: str = Field(..., description="Tenant UUID")


class APIKeyCreateResponse(BaseModel):
    api_key: str
    message: str = "Store this key securely — it cannot be retrieved again."


class TenantContext(BaseModel):
    """Injected into request.state by the auth middleware."""
    tenant_id: UUID
    tenant_name: str
    tier: str
    roles: list[str]
    allowed_block_ids: list[str]
    data_scopes: Optional[list[str]]
    rate_limit_rpm: int


# =============================================================================
# API KEY UTILITIES
# =============================================================================

def generate_api_key() -> str:
    """Generate a cryptographically secure 48-character API key."""
    return f"ml_{secrets.token_urlsafe(36)}"


def hash_api_key(api_key: str) -> str:
    """Hash an API key using bcrypt for storage."""
    return bcrypt.hashpw(api_key.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_api_key(api_key: str, hashed: str) -> bool:
    """Verify an API key against its bcrypt hash."""
    try:
        return bcrypt.checkpw(api_key.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


# =============================================================================
# JWT UTILITIES
# =============================================================================

def create_access_token(payload: dict[str, Any]) -> str:
    """Create a short-lived JWT access token (1 hour default)."""
    now = datetime.now(timezone.utc)
    token_data = {
        **payload,
        "iat": now,
        "exp": now + timedelta(minutes=JWT_EXPIRY_MINUTES),
        "type": "access",
    }
    return jwt.encode(token_data, JWT_SECRET, algorithm=JWT_ALGORITHM)


def create_refresh_token(tenant_id: str) -> str:
    """Create a longer-lived refresh token (24 hours default)."""
    now = datetime.now(timezone.utc)
    token_data = {
        "tenant_id": tenant_id,
        "iat": now,
        "exp": now + timedelta(hours=JWT_REFRESH_EXPIRY_HOURS),
        "type": "refresh",
        "jti": secrets.token_hex(16),  # Unique token ID
    }
    return jwt.encode(token_data, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_token(token: str, expected_type: str = "access") -> dict[str, Any]:
    """
    Decode and validate a JWT token.
    Raises HTTPException on invalid/expired tokens.
    """
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.InvalidTokenError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {e}",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if payload.get("type") != expected_type:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Expected {expected_type} token, got {payload.get('type')}",
        )

    return payload


# =============================================================================
# RATE LIMITER
# =============================================================================

class RateLimiter:
    """
    In-memory sliding window rate limiter, keyed by tenant_id.
    Each tenant gets a per-minute request quota based on their tier.
    """

    def __init__(self):
        # tenant_id → list of request timestamps (monotonic)
        self._windows: dict[str, list[float]] = defaultdict(list)
        self._last_cleanup: float = 0.0

    def check(self, tenant_id: str, limit_rpm: int) -> tuple[bool, int]:
        """
        Check if a tenant is within their rate limit.
        Returns (allowed, remaining_requests).
        """
        now = time.monotonic()
        window_start = now - 60.0  # 1-minute sliding window

        # Periodic cleanup of old entries (every 30s)
        if now - self._last_cleanup > 30.0:
            self._cleanup(window_start)
            self._last_cleanup = now

        # Get this tenant's recent requests
        requests = self._windows[tenant_id]

        # Prune expired entries
        requests[:] = [t for t in requests if t > window_start]

        if len(requests) >= limit_rpm:
            return False, 0

        # Record this request
        requests.append(now)
        remaining = limit_rpm - len(requests)
        return True, remaining

    def _cleanup(self, cutoff: float) -> None:
        """Remove expired entries from all tenants."""
        empty_tenants = []
        for tid, requests in self._windows.items():
            requests[:] = [t for t in requests if t > cutoff]
            if not requests:
                empty_tenants.append(tid)
        for tid in empty_tenants:
            del self._windows[tid]


# Singleton rate limiter
_rate_limiter = RateLimiter()


# =============================================================================
# AUDIT LOGGER
# =============================================================================

def log_audit(
    conn: psycopg2.extensions.connection,
    tenant_id: Optional[str],
    method: str,
    endpoint: str,
    status_code: int,
    ip_address: Optional[str],
    user_agent: Optional[str],
    duration_ms: Optional[int],
) -> None:
    """Write an audit log entry for an authenticated API request."""
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO audit_log (tenant_id, method, endpoint, status_code,
                                       ip_address, user_agent, duration_ms)
                VALUES (%s, %s, %s, %s, %s::INET, %s, %s)
            """, (tenant_id, method, endpoint, status_code, ip_address, user_agent, duration_ms))
        conn.commit()
    except Exception as e:
        log.warning(f"Failed to write audit log: {e}")
        try:
            conn.rollback()
        except Exception:
            pass


# =============================================================================
# AUTH MIDDLEWARE
# =============================================================================

class AuthMiddleware(BaseHTTPMiddleware):
    """
    Starlette middleware that:
    1. Extracts JWT from Authorization: Bearer header
    2. Validates the token and loads tenant context
    3. Resolves tenant's allowed blocks from tenant_blocks table
    4. Sets PostgreSQL session variables for RLS
    5. Enforces per-tenant rate limiting
    6. Logs every request to the audit table
    """

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        start_time = time.monotonic()

        # Skip auth for public paths and WebSocket upgrades
        path = request.url.path
        if path in PUBLIC_PATHS or path.startswith("/ws/"):
            # WebSocket auth is handled at connection time
            response = await call_next(request)
            return response

        # Extract Bearer token
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return Response(
                content=json.dumps({"detail": "Missing or invalid Authorization header"}),
                status_code=401,
                media_type="application/json",
                headers={"WWW-Authenticate": "Bearer"},
            )

        token = auth_header[7:]  # Strip "Bearer "

        # Decode JWT
        try:
            payload = decode_token(token, expected_type="access")
        except HTTPException as e:
            return Response(
                content=json.dumps({"detail": e.detail}),
                status_code=e.status_code,
                media_type="application/json",
            )

        tenant_id = payload.get("tenant_id")
        tier = payload.get("tier", "customer")
        rate_limit = payload.get("rate_limit_rpm", TIER_RATE_LIMITS.get(tier, 100))

        # Rate limit check
        allowed, remaining = _rate_limiter.check(tenant_id, rate_limit)
        if not allowed:
            elapsed_ms = int((time.monotonic() - start_time) * 1000)
            # Still log rate-limited requests
            self._audit(tenant_id, request, 429, elapsed_ms)
            return Response(
                content=json.dumps({"detail": "Rate limit exceeded", "retry_after_seconds": 60}),
                status_code=429,
                media_type="application/json",
                headers={
                    "Retry-After": "60",
                    "X-RateLimit-Limit": str(rate_limit),
                    "X-RateLimit-Remaining": "0",
                },
            )

        # Load tenant's allowed blocks
        allowed_blocks = self._load_allowed_blocks(tenant_id)

        # Inject tenant context into request.state
        request.state.tenant_id = tenant_id
        request.state.tenant_name = payload.get("tenant_name", "")
        request.state.tenant_tier = tier
        request.state.roles = payload.get("roles", [])
        request.state.allowed_block_ids = allowed_blocks
        request.state.data_scopes = TIER_DATA_SCOPES.get(tier)
        request.state.rate_limit_rpm = rate_limit

        # Call the actual endpoint
        response = await call_next(request)

        # Add rate limit headers
        response.headers["X-RateLimit-Limit"] = str(rate_limit)
        response.headers["X-RateLimit-Remaining"] = str(remaining)

        # Audit log
        elapsed_ms = int((time.monotonic() - start_time) * 1000)
        self._audit(tenant_id, request, response.status_code, elapsed_ms)

        return response

    @staticmethod
    def _load_allowed_blocks(tenant_id: str) -> list[str]:
        """Load the list of block IDs this tenant can access."""
        try:
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT block_id::TEXT FROM tenant_blocks
                        WHERE tenant_id = %s
                    """, (tenant_id,))
                    return [row[0] for row in cur.fetchall()]
        except Exception as e:
            log.error(f"Failed to load tenant blocks: {e}")
            return []

    @staticmethod
    def _audit(
        tenant_id: Optional[str],
        request: Request,
        status_code: int,
        duration_ms: int,
    ) -> None:
        """Write audit log entry (fire-and-forget)."""
        try:
            ip = request.client.host if request.client else None
            ua = request.headers.get("User-Agent", "")[:500]
            with get_db() as conn:
                log_audit(
                    conn, tenant_id,
                    request.method, request.url.path,
                    status_code, ip, ua, duration_ms,
                )
        except Exception as e:
            log.warning(f"Audit log write failed: {e}")


# =============================================================================
# ROLE-BASED ACCESS CONTROL DEPENDENCY
# =============================================================================

class require_roles:
    """
    FastAPI dependency that enforces role requirements on an endpoint.

    Usage:
        @app.post("/sites", dependencies=[Depends(require_roles("admin"))])
        @app.post("/alarms/{id}/ack", dependencies=[Depends(require_roles("admin", "operator"))])
    """

    def __init__(self, *roles: str):
        self.required_roles = set(roles)

    def __call__(self, request: Request) -> None:
        user_roles = set(getattr(request.state, "roles", []))
        # Admin always passes
        if "admin" in user_roles:
            return
        if not self.required_roles.intersection(user_roles):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires one of roles: {', '.join(sorted(self.required_roles))}",
            )


class require_tier:
    """
    FastAPI dependency that restricts endpoints to specific tenant tiers.

    Usage:
        @app.get("/internal", dependencies=[Depends(require_tier("internal"))])
    """

    def __init__(self, *tiers: str):
        self.allowed_tiers = set(tiers)

    def __call__(self, request: Request) -> None:
        tier = getattr(request.state, "tenant_tier", "")
        if tier not in self.allowed_tiers:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Endpoint restricted to tiers: {', '.join(sorted(self.allowed_tiers))}",
            )


# =============================================================================
# TENANT CONTEXT HELPERS (for use in endpoint code)
# =============================================================================

def get_tenant_ctx(request: Request) -> TenantContext:
    """Extract the full tenant context from request.state."""
    return TenantContext(
        tenant_id=UUID(request.state.tenant_id),
        tenant_name=request.state.tenant_name,
        tier=request.state.tenant_tier,
        roles=request.state.roles,
        allowed_block_ids=request.state.allowed_block_ids,
        data_scopes=request.state.data_scopes,
        rate_limit_rpm=request.state.rate_limit_rpm,
    )


def verify_block_access(request: Request, block_id: str) -> None:
    """
    Verify that the current tenant has access to a specific block.
    Internal tier bypasses this check.
    """
    tier = getattr(request.state, "tenant_tier", "internal")
    if tier == "internal":
        return

    allowed = getattr(request.state, "allowed_block_ids", [])
    if str(block_id) not in allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Access denied to block {block_id}",
        )


# =============================================================================
# AUTH ROUTER
# =============================================================================

auth_router = APIRouter(prefix="/auth", tags=["Auth"])


# --------------------------------------------------------------------------
# POST /auth/token — exchange API key for JWT
# --------------------------------------------------------------------------

@auth_router.post("/token", response_model=TokenResponse)
async def exchange_token(
    body: TokenRequest,
    conn: psycopg2.extensions.connection = Depends(db_dep),
):
    """
    Exchange an API key for a short-lived JWT access token (1 hour)
    and a refresh token (24 hours).

    The API key is verified against the bcrypt hash stored in the tenants table.
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        # Load all active tenants and check API key
        # (In production, consider a lookup index or key prefix)
        cur.execute("""
            SELECT id::TEXT, name, tier::TEXT, api_key_hash, roles, rate_limit_rpm
            FROM tenants
            WHERE is_active = TRUE
        """)
        tenants = cur.fetchall()

    matched_tenant = None
    for tenant in tenants:
        if verify_api_key(body.api_key, tenant["api_key_hash"]):
            matched_tenant = tenant
            break

    if matched_tenant is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )

    # Build JWT payload
    jwt_payload = {
        "tenant_id": matched_tenant["id"],
        "tenant_name": matched_tenant["name"],
        "tier": matched_tenant["tier"],
        "roles": matched_tenant["roles"] or [],
        "rate_limit_rpm": matched_tenant["rate_limit_rpm"],
    }

    access_token = create_access_token(jwt_payload)
    refresh_token = create_refresh_token(matched_tenant["id"])

    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=JWT_EXPIRY_MINUTES * 60,
        tenant_id=matched_tenant["id"],
        tenant_name=matched_tenant["name"],
        tier=matched_tenant["tier"],
        roles=matched_tenant["roles"] or [],
    )


# --------------------------------------------------------------------------
# POST /auth/refresh — refresh an access token
# --------------------------------------------------------------------------

@auth_router.post("/refresh", response_model=RefreshResponse)
async def refresh_token(
    body: RefreshRequest,
    conn: psycopg2.extensions.connection = Depends(db_dep),
):
    """
    Exchange a valid refresh token for a new access token.

    The refresh token must not be expired. A new access token is issued
    with the latest tenant data from the database.
    """
    # Decode refresh token
    payload = decode_token(body.refresh_token, expected_type="refresh")
    tenant_id = payload.get("tenant_id")

    # Reload tenant data (roles/tier may have changed)
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT id::TEXT, name, tier::TEXT, roles, rate_limit_rpm, is_active
            FROM tenants
            WHERE id = %s
        """, (tenant_id,))
        tenant = cur.fetchone()

    if not tenant or not tenant["is_active"]:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Tenant not found or deactivated",
        )

    jwt_payload = {
        "tenant_id": tenant["id"],
        "tenant_name": tenant["name"],
        "tier": tenant["tier"],
        "roles": tenant["roles"] or [],
        "rate_limit_rpm": tenant["rate_limit_rpm"],
    }

    new_access_token = create_access_token(jwt_payload)

    return RefreshResponse(
        access_token=new_access_token,
        expires_in=JWT_EXPIRY_MINUTES * 60,
    )


# --------------------------------------------------------------------------
# POST /auth/keys — generate new API key (admin only)
# --------------------------------------------------------------------------

@auth_router.post(
    "/keys",
    response_model=APIKeyCreateResponse,
    dependencies=[Depends(require_roles("admin"))],
)
async def create_api_key(
    request: Request,
    body: APIKeyCreateRequest,
    conn: psycopg2.extensions.connection = Depends(db_dep),
):
    """
    Generate a new API key for a tenant (admin only).

    The raw key is returned exactly once. It is stored as a bcrypt hash
    and cannot be retrieved again.
    """
    raw_key = generate_api_key()
    hashed = hash_api_key(raw_key)

    with conn.cursor() as cur:
        cur.execute("""
            UPDATE tenants SET api_key_hash = %s, updated_at = now()
            WHERE id = %s AND is_active = TRUE
            RETURNING id
        """, (hashed, body.tenant_id))
        if not cur.fetchone():
            conn.rollback()
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Tenant not found or inactive")
        conn.commit()

    log.info(f"API key regenerated for tenant {body.tenant_id}")

    return APIKeyCreateResponse(api_key=raw_key)


# --------------------------------------------------------------------------
# GET /auth/me — current tenant info
# --------------------------------------------------------------------------

@auth_router.get("/me")
async def auth_me(request: Request):
    """Return the current authenticated tenant context (from JWT)."""
    return {
        "tenant_id": getattr(request.state, "tenant_id", None),
        "tenant_name": getattr(request.state, "tenant_name", None),
        "tier": getattr(request.state, "tenant_tier", None),
        "roles": getattr(request.state, "roles", []),
        "allowed_blocks": getattr(request.state, "allowed_block_ids", []),
        "data_scopes": getattr(request.state, "data_scopes", None),
        "rate_limit_rpm": getattr(request.state, "rate_limit_rpm", None),
    }
