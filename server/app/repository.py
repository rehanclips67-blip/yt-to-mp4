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
