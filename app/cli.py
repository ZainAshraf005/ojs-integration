"""Admin command line.  Usage:  python -m app.cli <command> --help

  create-user EMAIL                 admin login for /admin
  add-source NAME BASE_URL          register an OJS install (asks for its API key)
  list-sources
  plugin-config SOURCE_ID           print the config.inc.php block for that install's plugin
  backfill SOURCE_ID [--journal P]  import everything now, showing progress (no worker needed)
  queue-backfill SOURCE_ID          same, but hand it to the background worker
  sync-article SOURCE_ID PATH ID    re-sync a single article now
  push-ids SOURCE_ID                write article IDs into OJS now (needs archiveSync plugin 1.1+)
  check-health [SOURCE_ID]          run health checks now
"""

import argparse
import asyncio
import getpass
import secrets
import sys

from sqlalchemy import select

from app import health, sync
from app.config import get_settings
from app.db import SessionLocal, engine
from app.models import Journal, Source, User
from app.ojs.client import OJSClient, OJSError
from app.security import encrypt, hash_password


async def create_user(args) -> None:
    password = getpass.getpass("Password (min 12 chars): ")
    if len(password) < 12:
        sys.exit("Password too short.")
    async with SessionLocal() as session:
        email = args.email.strip().lower()
        user = await session.scalar(select(User).where(User.email == email))
        if user:
            user.password_hash = hash_password(password)
            print(f"Updated password for {email}")
        else:
            session.add(User(email=email, password_hash=hash_password(password)))
            print(f"Created admin user {email}")
        await session.commit()


def _plugin_config(source: Source) -> str:
    return (
        "[archivesync]\n"
        f'webhook_url = "{get_settings().public_base_url}/hooks/ojs/{source.id}"\n'
        f'webhook_secret = "{source.webhook_secret}"\n'
    )


async def add_source(args) -> None:
    base_url = args.base_url.rstrip("/")
    token = args.token or getpass.getpass("OJS API key: ")
    print(f"Testing connection to {base_url} ...")
    try:
        async with OJSClient(base_url, token, delay=0) as ojs:
            count = 0
            async for _ in ojs.contexts():
                count += 1
    except OJSError as exc:
        sys.exit(f"Could not connect: {exc}")
    print(f"OK: {count} journal(s) found.")

    async with SessionLocal() as session:
        if await session.scalar(select(Source).where(Source.base_url == base_url)):
            sys.exit("A source with this URL already exists.")
        source = Source(name=args.name, base_url=base_url, api_token_enc=encrypt(token),
                        webhook_secret=secrets.token_hex(32))
        session.add(source)
        await session.commit()
        print(f"\nAdded source #{source.id} ({source.name}).")
        print("\nAdd this to the END of config.inc.php on that OJS install:\n")
        print(_plugin_config(source))
        print(f"Next: python -m app.cli backfill {source.id}")


async def list_sources(args) -> None:
    async with SessionLocal() as session:
        for s in await session.scalars(select(Source).order_by(Source.id)):
            journals = len(list(await session.scalars(select(Journal.id).where(Journal.source_id == s.id))))
            state = "up" if s.is_up else "DOWN"
            print(f"#{s.id:<3} {s.name:<24} {s.base_url:<55} journals={journals:<4} {state}"
                  f"{'' if s.enabled else ' (disabled)'}")


async def plugin_config(args) -> None:
    async with SessionLocal() as session:
        source = await session.get(Source, args.source_id)
        if not source:
            sys.exit("No such source.")
        print(_plugin_config(source))


async def backfill(args) -> None:
    async with SessionLocal() as session:
        source = await session.get(Source, args.source_id)
        if not source:
            sys.exit("No such source.")
        async with sync.client_for(source) as ojs:
            journals = await sync.discover_journals(session, source, ojs)
            print(f"{len(journals)} journal(s) in {source.name}")
            total_files = 0
            for journal in journals:
                if not journal.enabled or (args.journal and journal.ojs_path != args.journal):
                    continue
                todo = await sync.find_changes(session, journal, ojs)
                if not todo:
                    continue
                print(f"\n[{journal.number}] {journal.ojs_path}: {len(todo)} article(s) to sync")
                for i, submission_id in enumerate(todo, 1):
                    try:
                        new = await sync.sync_article(session, source, journal, ojs, submission_id)
                        total_files += len(new)
                        print(f"  {i}/{len(todo)}  {journal.number}/{submission_id}  +{len(new)} file(s)")
                    except Exception as exc:  # keep going; the error is in the sync log
                        await session.rollback()
                        await sync.record(session, "error", f"sync failed: {exc}", level="error",
                                          source_id=source.id, ref=f"{journal.ojs_path}/{submission_id}")
                        await session.commit()
                        print(f"  {i}/{len(todo)}  {journal.number}/{submission_id}  ERROR {exc}")
            print(f"\nDone. {total_files} new file(s) stored.")


async def queue_backfill(args) -> None:
    from arq import create_pool
    from arq.connections import RedisSettings

    redis = await create_pool(RedisSettings.from_dsn(get_settings().redis_url))
    await redis.enqueue_job("reconcile_source_job", args.source_id, _job_id=f"reconcile:{args.source_id}")
    await redis.aclose()
    print("Queued. Watch progress in /admin → Sync log, or the worker's output.")


async def sync_article(args) -> None:
    async with SessionLocal() as session:
        source = await session.get(Source, args.source_id)
        journal = await session.scalar(
            select(Journal).where(Journal.source_id == args.source_id, Journal.ojs_path == args.journal_path)
        )
        if not source or not journal:
            sys.exit("Unknown source or journal (run backfill once to discover journals).")
        async with sync.client_for(source) as ojs:
            new = await sync.sync_article(session, source, journal, ojs, args.submission_id)
        print(f"Synced {journal.number}/{args.submission_id}: {len(new)} new file(s)")


async def push_ids(args) -> None:
    async with SessionLocal() as session:
        source = await session.get(Source, args.source_id)
        if not source:
            sys.exit("No such source.")
        try:
            async with sync.client_for(source) as ojs:
                stored = await sync.push_identifiers(session, source, ojs)
        except OJSError as exc:
            await session.commit()  # keep what was confirmed before the failure
            sys.exit(f"Stopped: {exc}")
        await session.commit()
        print(f"{stored} article ID(s) written to OJS. Anything OJS refused is in /admin → Sync log.")


async def check_health(args) -> None:
    async with SessionLocal() as session:
        query = select(Source).where(Source.enabled.is_(True))
        if args.source_id:
            query = query.where(Source.id == args.source_id)
        for source in await session.scalars(query):
            up = await health.check_source(session, source)
            line = f"#{source.id} {source.name}: {'up' if up else 'DOWN ' + (source.last_error or '')}"
            if up:
                checked, failing = await health.check_articles(session, source)
                line += f", articles checked={checked} failing={failing}"
            print(line)


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("create-user"); p.add_argument("email"); p.set_defaults(fn=create_user)
    p = sub.add_parser("add-source"); p.add_argument("name"); p.add_argument("base_url")
    p.add_argument("--token", help="API key (default: prompt)"); p.set_defaults(fn=add_source)
    p = sub.add_parser("list-sources"); p.set_defaults(fn=list_sources)
    p = sub.add_parser("plugin-config"); p.add_argument("source_id", type=int); p.set_defaults(fn=plugin_config)
    p = sub.add_parser("backfill"); p.add_argument("source_id", type=int); p.add_argument("--journal")
    p.set_defaults(fn=backfill)
    p = sub.add_parser("queue-backfill"); p.add_argument("source_id", type=int); p.set_defaults(fn=queue_backfill)
    p = sub.add_parser("sync-article"); p.add_argument("source_id", type=int); p.add_argument("journal_path")
    p.add_argument("submission_id", type=int); p.set_defaults(fn=sync_article)
    p = sub.add_parser("push-ids"); p.add_argument("source_id", type=int); p.set_defaults(fn=push_ids)
    p = sub.add_parser("check-health"); p.add_argument("source_id", type=int, nargs="?"); p.set_defaults(fn=check_health)

    args = parser.parse_args()

    async def run():
        try:
            await args.fn(args)
        finally:
            await engine.dispose()

    asyncio.run(run())


if __name__ == "__main__":
    main()
