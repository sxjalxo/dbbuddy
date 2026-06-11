from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from dbbuddy_core.models import DBConfig
from dbbuddy_core.pipeline import process_query, process_schema

app = FastAPI(title="dbbuddy API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class AnalyzeRequest(BaseModel):
    host: str
    user: str
    password: str
    database: str
    ai: bool = False
    ai_provider: str = "local"


class QueryRequest(BaseModel):
    host: str
    user: str
    password: str
    database: str
    question: str
    ai: bool = True
    ai_provider: str = "openai"


class ExecuteRequest(BaseModel):
    host: str
    user: str
    password: str
    database: str
    sql: str


@app.get("/")
def health_check():
    return {"status": "ok"}


@app.post("/analyze")
def analyze(req: AnalyzeRequest):
    try:
        config = DBConfig(
            host=req.host,
            user=req.user,
            password=req.password,
            database=req.database,
            ai=req.ai,
            ai_provider=req.ai_provider,
        )
        return process_schema(config)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/query")
def query(req: QueryRequest):
    try:
        config = DBConfig(
            host=req.host,
            user=req.user,
            password=req.password,
            database=req.database,
            ai=req.ai,
            ai_provider=req.ai_provider,
        )
        return process_query(config, req.question)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/execute")
def execute(req: ExecuteRequest):
    try:
        from dbbuddy_core.db import connect_db
        from dbbuddy_core.query import execute_query as run_query

        conn = connect_db(req.host, req.user, req.password, req.database)
        if conn is None:
            raise RuntimeError("Unable to connect to the database.")

        results = run_query(conn, req.sql)
        return {"results": results}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
