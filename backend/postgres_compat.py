"""Small DB-API compatibility layer for the existing SQLite-shaped repositories."""
import re
import threading
from datetime import date, datetime
from decimal import Decimal
from collections.abc import Mapping
from typing import Iterable

from psycopg2.extras import RealDictCursor, execute_batch
from psycopg2.pool import ThreadedConnectionPool


ID_TABLES={"users","holdings","trades","agent_runs","watchlist_snapshots","stock_analyses",
           "sentiment_snapshots","news_articles","news_ingestion_logs","ai_gateway_logs",
           "derivative_plans","bot_conversations","bot_messages","monitoring_events",
           "order_intents","order_events","auth_login_attempts"}
_pool=None; _lock=threading.Lock()


def _coerce(value):
    if isinstance(value,Decimal): return float(value)
    if isinstance(value,(datetime,date)): return value.isoformat()
    return value


class CompatRow(Mapping):
    def __init__(self, value): self._data={key:_coerce(item) for key,item in dict(value).items()}; self._keys=list(self._data)
    def __getitem__(self,key): return self._data[self._keys[key]] if isinstance(key,int) else self._data[key]
    def __iter__(self): return iter(self._data)
    def __len__(self): return len(self._data)
    def __repr__(self): return repr(self._data)


class CompatCursor:
    def __init__(self, rows=None, lastrowid=None, rowcount=-1):
        self._rows=[CompatRow(row) for row in (rows or [])]; self._index=0
        self.lastrowid=lastrowid; self.rowcount=rowcount
    def fetchone(self):
        if self._index>=len(self._rows): return None
        row=self._rows[self._index]; self._index+=1; return row
    def fetchall(self):
        rows=self._rows[self._index:]; self._index=len(self._rows); return rows
    def __iter__(self): return iter(self.fetchall())


def _adapt(sql: str):
    statement=sql.strip().rstrip(";")
    ignored=bool(re.match(r"(?is)^INSERT\s+OR\s+IGNORE\s+INTO",statement))
    statement=re.sub(r"(?is)^INSERT\s+OR\s+IGNORE\s+INTO","INSERT INTO",statement)
    statement=statement.replace("?","%s")
    if ignored and " ON CONFLICT " not in statement.upper(): statement += " ON CONFLICT DO NOTHING"
    match=re.match(r"(?is)^INSERT\s+INTO\s+([a-z_]+)",statement)
    table=match.group(1).lower() if match else ""
    returns_id=table in ID_TABLES and " RETURNING " not in statement.upper()
    if returns_id: statement += " RETURNING id"
    return statement,returns_id


def pool(database_url: str, minimum: int = 1, maximum: int = 12):
    global _pool
    with _lock:
        if _pool is None: _pool=ThreadedConnectionPool(minimum,maximum,dsn=database_url.replace("postgresql+psycopg2://","postgresql://",1))
    return _pool


class PostgresConnection:
    is_postgres = True
    def __init__(self,database_url: str,minimum: int=1,maximum: int=12):
        self._pool=pool(database_url,minimum,maximum); self._connection=self._pool.getconn(); self._closed=False
    @property
    def row_factory(self): return None
    @row_factory.setter
    def row_factory(self,value): pass
    def execute(self,sql: str,params=()):
        statement,returns_id=_adapt(sql)
        with self._connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(statement,tuple(params or ()))
            rows=cursor.fetchall() if cursor.description else []
            lastrowid=rows[0].get("id") if returns_id and rows else None
            return CompatCursor(rows,lastrowid,cursor.rowcount)
    def executemany(self,sql: str,values: Iterable):
        statement,_=_adapt(sql)
        with self._connection.cursor() as cursor: execute_batch(cursor,statement,list(values),page_size=1000); return CompatCursor(rowcount=cursor.rowcount)
    def commit(self): self._connection.commit()
    def rollback(self): self._connection.rollback()
    def close(self):
        if not self._closed: self._pool.putconn(self._connection); self._closed=True
    def __enter__(self): return self
    def __exit__(self,exc_type,exc,tb):
        self.rollback() if exc_type else self.commit(); self.close(); return False
