"""Operational CLI: `python -m app.cli <command>`.

  create-admin --email E            (password from ADMIN_PASSWORD env or prompt)
  enrollment-token --name NAME      prints a single-use agent enrollment token
  add-account --email E --attest T  registers an AUTHORIZED provider account
  init-pki --out DIR                creates CA + gateway server certificate
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
from datetime import timedelta

from sqlalchemy import select

from app.core.config import get_settings
from app.core.db import dispose_engine, session_factory
from app.core.security import hash_password, hash_token, new_opaque_token, utcnow
from app.models import AuthorizationStatus, EnrollmentToken, GoogleAccount, User, UserRole
from app.services import credit_service
from app.services.audit import audit


async def create_admin(email: str, password: str) -> None:
    async with session_factory()() as db:
        user = (await db.execute(select(User).where(User.email == email.lower()))).scalar_one_or_none()
        if user is None:
            user = User(email=email.lower(), password_hash=hash_password(password), role=UserRole.ADMIN.value)
            db.add(user)
            await db.flush()
            await credit_service.get_or_create_wallet(db, user.id)
        else:
            user.role = UserRole.ADMIN.value
            user.password_hash = hash_password(password)
        audit(db, action="admin.bootstrap", resource_type="user", resource_id=user.id, actor_type="system")
        await db.commit()
        print(f"admin ready: {user.email} ({user.id})")


async def enrollment_token(name: str) -> None:
    raw = new_opaque_token(32)
    async with session_factory()() as db:
        db.add(EnrollmentToken(token_hash=hash_token(raw), workstation_name=name,
                               expires_at=utcnow() + timedelta(seconds=get_settings().enrollment_token_ttl_seconds)))
        audit(db, action="enrollment_token.create", resource_type="enrollment_token", actor_type="system",
              details={"workstation_name": name})
        await db.commit()
    print(raw)


async def add_account(email: str, attestation: str, owner_email: str | None) -> None:
    async with session_factory()() as db:
        q = select(User).where(User.role == UserRole.ADMIN.value)
        if owner_email:
            q = select(User).where(User.email == owner_email.lower())
        owner = (await db.execute(q.limit(1))).scalar_one_or_none()
        if owner is None:
            sys.exit("no admin/owner user found; run create-admin first")
        acct = (await db.execute(select(GoogleAccount).where(GoogleAccount.google_email == email.lower()))
                ).scalar_one_or_none()
        if acct is None:
            acct = GoogleAccount(google_email=email.lower(), owner_id=owner.id)
            db.add(acct)
        acct.authorization_status = AuthorizationStatus.AUTHORIZED.value
        await db.flush()
        audit(db, action="google_account.authorize", resource_type="google_account", resource_id=acct.id,
              actor_id=owner.id, actor_type="system", details={"attestation": attestation})
        await db.commit()
        print(f"account authorized: {acct.google_email} ({acct.id})")


def main() -> None:
    p = argparse.ArgumentParser(prog="app.cli")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("create-admin")
    a.add_argument("--email", required=True)
    e = sub.add_parser("enrollment-token")
    e.add_argument("--name", required=True)
    g = sub.add_parser("add-account")
    g.add_argument("--email", required=True)
    g.add_argument("--attest", required=True, help="Why automated use of this account is authorized")
    g.add_argument("--owner-email")
    k = sub.add_parser("init-pki")
    k.add_argument("--out", default="/certs")
    k.add_argument("--server-name", action="append", dest="names")
    k.add_argument("--force", action="store_true")
    args = p.parse_args()

    if args.cmd == "init-pki":
        from app.workers.pki import init_pki
        init_pki(args.out, args.names or ["localhost", "agent-gateway", "127.0.0.1"], force=args.force)
        return

    async def run() -> None:
        try:
            if args.cmd == "create-admin":
                pw = os.environ.get("ADMIN_PASSWORD") or getpass.getpass("Admin password: ")
                if len(pw) < 12:
                    sys.exit("admin password must be at least 12 characters")
                await create_admin(args.email, pw)
            elif args.cmd == "enrollment-token":
                await enrollment_token(args.name)
            elif args.cmd == "add-account":
                await add_account(args.email, args.attest, args.owner_email)
        finally:
            await dispose_engine()

    asyncio.run(run())


if __name__ == "__main__":
    main()
