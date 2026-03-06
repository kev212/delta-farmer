# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Crafted with love and ctrl+c
from datetime import UTC, datetime, timedelta
from typing import Awaitable, Callable, Generic, Type, TypeVar, cast

from pydantic import BaseModel

from .logger import logger
from .utils import pickle_dump, pickle_load

T = TypeVar("T")
FetchFn = Callable[[datetime | None], Awaitable[list[T]]]


class DataStore(Generic[T]):
    def __init__(self, filepath: str, id_key: str = "id", model: Type[T] | None = None):
        self.filepath = filepath
        self.last_dt: datetime | None = None
        self.records: dict[str, T] = {}
        self.id_key = id_key
        self.model = model
        self._load()

    def _load(self):
        data = pickle_load(self.filepath, delete_on_error=True)
        if not data:
            return

        try:
            self.last_dt = data.get("last_sync")
            raw: dict[str, dict] = data.get("records", {})
            if self.model is not None:
                self.records = {k: self.model.model_validate(v) for k, v in raw.items()}  # type: ignore
            else:
                self.records = cast(dict[str, T], raw)
        except Exception as e:
            logger.warning(f"Failed to deserialize {self.filepath.split('/')[-1]}, dropping data")
            logger.error(e)
            exit(0)
            self.last_dt = None
            self.records = {}

    def save(self):
        raw = {
            k: v.model_dump() if isinstance(v, BaseModel) else v for k, v in self.records.items()
        }
        pickle_dump(self.filepath, {"last_sync": self.last_dt, "records": raw})

    def upsert(self, records: list[T]):
        for record in records:
            try:
                key = str(
                    getattr(record, self.id_key)
                    if isinstance(record, BaseModel)
                    else cast(dict, record)[self.id_key]
                )
            except (KeyError, AttributeError):
                logger.error(f"Record is missing id_key '{self.id_key}': {record}")
                raise
            self.records[key] = record

    def count(self) -> int:
        return len(self.records)

    def get_all(self) -> list[T]:
        return list(self.records.values())

    def needs_sync(self, ttl_sec: int) -> bool:
        if self.last_dt is None:
            return True
        age = (datetime.now(tz=UTC) - self.last_dt).total_seconds()
        return age >= ttl_sec

    def update_sync_time(self, dt: datetime | None = None):
        self.last_dt = dt or datetime.now(tz=UTC)

    def get_last_sync(self) -> datetime | None:
        return self.last_dt

    async def sync(self, fetch_fn: FetchFn, ttl_sec=3600, lookback_sec=60) -> "DataStore[T]":
        if not self.needs_sync(ttl_sec) and self.last_dt is not None:
            df = self.last_dt.strftime("%Y-%m-%d %H:%M")
            logger.trace(f"No sync needed for {self.filepath.split('/')[-1]} (last: {df})")
            return self

        since = self.last_dt
        since = since - timedelta(seconds=lookback_sec) if since else None

        df = since.strftime("%Y-%m-%d %H:%M") if since else "beginning"
        logger.trace(f"Syncing data for {self.filepath.split('/')[-1]} (last: {df})...")

        records = await fetch_fn(since)
        self.upsert(records)
        self.update_sync_time()
        self.save()
        return self
