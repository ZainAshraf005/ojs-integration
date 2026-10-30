"""Database tables.

Hierarchy mirrors OJS:  Source (an OJS install) -> Journal -> Article -> Version -> File

Public identifier:  {PUBLIC_BASE_URL}/{journal.number}/{article.number}  (always our page)
Link shown in OJS:  the same with ?redirect  (the journal's page while it's reachable)
Both numbers are stored once and never recalculated, so IDs stay valid even if
the OJS path, install or internal IDs change later.
"""

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.config import get_settings

JOURNAL_NUMBER_START = 1001


def pid_url(journal_number: int, article_number: int | None = None) -> str:
    """The public persistent identifier, e.g. https://doi-services.com/1003/188"""
    base = f"{get_settings().public_base_url}/{journal_number}"
    return base if article_number is None else f"{base}/{article_number}"


REDIRECT_PARAM = "redirect"


def redirect_url(journal_number: int, article_number: int) -> str:
    """The link written into OJS: opens the article on the journal's site while it's reachable, else our copy."""
    return f"{pid_url(journal_number, article_number)}?{REDIRECT_PARAM}"


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Source(TimestampMixin, Base):
    """One OJS installation (e.g. https://example.com/journal)."""

    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    base_url: Mapped[str] = mapped_column(String(500), unique=True)
    api_token_enc: Mapped[str] = mapped_column(Text)
    webhook_secret: Mapped[str] = mapped_column(String(128))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # Health of the whole install (homepage reachable?)
    is_up: Mapped[bool] = mapped_column(Boolean, default=True)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    last_reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    journals: Mapped[list["Journal"]] = relationship(back_populates="source")

    def __str__(self) -> str:
        return self.name


class Journal(TimestampMixin, Base):
    __tablename__ = "journals"
    __table_args__ = (UniqueConstraint("source_id", "ojs_context_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[int] = mapped_column(Integer, unique=True)  # public, permanent
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))
    ojs_context_id: Mapped[int] = mapped_column(Integer)
    ojs_path: Mapped[str] = mapped_column(String(255))
    name: Mapped[str] = mapped_column(Text)
    names: Mapped[dict] = mapped_column(JSONB, default=dict)  # all locales
    acronym: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)  # archive this journal?

    source: Mapped[Source] = relationship(back_populates="journals")
    articles: Mapped[list["Article"]] = relationship(back_populates="journal")

    def __str__(self) -> str:
        return f"{self.number} · {self.name}"


class Article(TimestampMixin, Base):
    __tablename__ = "articles"
    __table_args__ = (
        UniqueConstraint("journal_id", "number"),
        UniqueConstraint("journal_id", "ojs_submission_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    journal_id: Mapped[int] = mapped_column(ForeignKey("journals.id"))
    number: Mapped[int] = mapped_column(Integer)  # public, permanent (= OJS id at import)
    ojs_submission_id: Mapped[int] = mapped_column(Integer)

    title: Mapped[str] = mapped_column(Text, default="")
    authors: Mapped[str] = mapped_column(Text, default="")
    date_published: Mapped[date | None] = mapped_column(Date)
    doi: Mapped[str | None] = mapped_column(String(255), index=True)

    # "published" while live in OJS; "withdrawn" if OJS unpublished it (we keep the copy)
    status: Mapped[str] = mapped_column(String(20), default="published")
    origin_url: Mapped[str | None] = mapped_column(String(1000))
    ojs_last_modified: Mapped[str | None] = mapped_column(String(32))
    # When OJS (archiveSync plugin) confirmed it stores our ID; None = still to be written there
    ojs_id_pushed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Health of the original article page
    origin_ok: Mapped[bool] = mapped_column(Boolean, default=True)
    origin_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    journal: Mapped[Journal] = relationship(back_populates="articles")
    versions: Mapped[list["ArticleVersion"]] = relationship(
        back_populates="article", order_by="ArticleVersion.version"
    )

    def __str__(self) -> str:
        return f"{self.journal_id}/{self.number} {self.title[:60]}"


class ArticleVersion(TimestampMixin, Base):
    """One OJS publication (version). Never overwritten once stored with files."""

    __tablename__ = "article_versions"
    __table_args__ = (UniqueConstraint("article_id", "ojs_publication_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id"))
    ojs_publication_id: Mapped[int] = mapped_column(Integer)
    version: Mapped[int] = mapped_column(Integer, default=1)
    date_published: Mapped[date | None] = mapped_column(Date)
    origin_url: Mapped[str | None] = mapped_column(String(1000))
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)
    metadata_hash: Mapped[str] = mapped_column(String(64), default="")

    article: Mapped[Article] = relationship(back_populates="versions")
    files: Mapped[list["File"]] = relationship(back_populates="version")

    def __str__(self) -> str:
        return f"v{self.version} (pub {self.ojs_publication_id})"


class File(TimestampMixin, Base):
    """A galley file (usually the PDF), stored by its SHA-256."""

    __tablename__ = "files"
    __table_args__ = (UniqueConstraint("version_id", "ojs_galley_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    version_id: Mapped[int] = mapped_column(ForeignKey("article_versions.id"))
    ojs_galley_id: Mapped[int] = mapped_column(Integer)
    ojs_submission_file_id: Mapped[int | None] = mapped_column(Integer)
    label: Mapped[str] = mapped_column(String(100), default="")
    original_name: Mapped[str] = mapped_column(String(500), default="")
    mimetype: Mapped[str] = mapped_column(String(100), default="")
    source_url: Mapped[str] = mapped_column(String(1000), default="")

    # "stored" | "remote" (galley is just an external link) | "failed"
    status: Mapped[str] = mapped_column(String(20), default="stored")
    error: Mapped[str | None] = mapped_column(Text)
    size: Mapped[int | None] = mapped_column(BigInteger)
    sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    storage_key: Mapped[str | None] = mapped_column(String(200))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    replicated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    version: Mapped[ArticleVersion] = relationship(back_populates="files")

    def __str__(self) -> str:
        return f"{self.label} {self.original_name}"


class SyncEvent(Base):
    """Audit log: every webhook, sync, error and conflict."""

    __tablename__ = "sync_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id"))
    kind: Mapped[str] = mapped_column(String(40))  # webhook, sync, conflict, error, ...
    level: Mapped[str] = mapped_column(String(10), default="info")  # info | warning | error
    ref: Mapped[str] = mapped_column(String(200), default="")  # e.g. "jest/188"
    message: Mapped[str] = mapped_column(Text, default="")
    data: Mapped[dict] = mapped_column(JSONB, default=dict)


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    def __str__(self) -> str:
        return self.email
