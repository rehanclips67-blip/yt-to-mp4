"""Small repository/adapter around SQLModel, keeping persistence out of jobs."""

import json
from datetime import UTC, datetime

from sqlmodel import Session, select

from .models import JobRecord, Source


def utcnow() -> datetime:
    return datetime.now(UTC)


class JobRepository:
    def __init__(self, engine):
        self.engine = engine

    def add(self, record: JobRecord) -> JobRecord:
        with Session(self.engine) as session:
            session.add(record)
            session.commit()
            session.refresh(record)
        return record

    def add_source(self, source: Source) -> Source:
        with Session(self.engine) as session:
            session.add(source)
            session.commit()
            session.refresh(source)
        return source

    def get_source(self, original_url: str) -> Source | None:
        with Session(self.engine) as session:
            query = (
                select(Source)
                .where(
                    Source.original_url == original_url,
                    Source.status == "ready",
                    Source.expires_at > utcnow(),
                )
                .order_by(Source.created_at.desc())
            )
            return session.exec(query).first()

    def get_source_by_id(self, source_id: str) -> Source | None:
        with Session(self.engine) as session:
            return session.get(Source, source_id)

    def update_source(self, source_id: str, **values) -> Source | None:
        with Session(self.engine) as session:
            source = session.get(Source, source_id)
            if source is None:
                return None
            for key, value in values.items():
                setattr(source, key, value)
            session.add(source)
            session.commit()
            session.refresh(source)
            return source

    def delete_source(self, source_id: str) -> bool:
        with Session(self.engine) as session:
            source = session.get(Source, source_id)
            if source is None:
                return False
            session.delete(source)
            session.commit()
            return True

    def expired_sources(self) -> list[Source]:
        with Session(self.engine) as session:
            return list(session.exec(select(Source).where(Source.expires_at <= utcnow())))

    def delete_expired_sources(self) -> list[Source]:
        with Session(self.engine) as session:
            expired = list(session.exec(select(Source).where(Source.expires_at <= utcnow())))
            for source in expired:
                session.delete(source)
            session.commit()
            return expired

    def get(self, job_id: str) -> JobRecord | None:
        with Session(self.engine) as session:
            return session.get(JobRecord, job_id)

    def list(self, kind: str | None = None) -> list[JobRecord]:
        with Session(self.engine) as session:
            query = select(JobRecord)
            if kind:
                query = query.where(JobRecord.kind == kind)
            return list(session.exec(query))

    def update(self, job_id: str, **values) -> JobRecord | None:
        with Session(self.engine) as session:
            record = session.get(JobRecord, job_id)
            if record is None:
                return None
            for key, value in values.items():
                setattr(record, key, value)
            session.add(record)
            session.commit()
            session.refresh(record)
            return record

    @staticmethod
    def encode_spec(spec) -> str:
        return json.dumps(spec, separators=(",", ":"))

    @staticmethod
    def decode_spec(record: JobRecord) -> dict:
        return json.loads(record.spec_json)
