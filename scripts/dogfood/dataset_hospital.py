"""Second dogfood dataset: a hospital/clinical schema that stresses what the ERP one did not.

Deliberately different along the axes that matter:

* **No declared foreign keys at all.** SQLite only records a FK when the DDL says
  ``REFERENCES``; omitting them forces the join graph onto the naming heuristic,
  which is the path a MyISAM MySQL or a warehouse extract actually takes. The ERP
  dataset declared every FK, so that path went ungraded.
* **Singular table names throughout** (``patient``, not ``patients``) — the
  spelling the live builder used to drop entirely.
* **Snowflake depth**: ``observation -> encounter -> patient -> practice ->
  region`` needs a four-hop path, where the ERP set rarely needed more than two.
* **A composite natural key** (``encounter_diagnosis``) with no surrogate ``id``,
  so anything assuming an ``id`` column has nowhere to hide.
* **A many-to-many with payload** (``encounter_diagnosis.rank``).
* **Column names that collide across tables** — ``name`` on four tables,
  ``code`` on three — so an unqualified reference is genuinely ambiguous.
* **A measure that is legitimately zero and one that is legitimately negative**
  (``adjustment``), because "sum is 0" is a real answer here, not only a bug.
* **Dates stored as TEXT** and a boolean stored as INTEGER, the SQLite reality.
"""

from __future__ import annotations

import os
import random
import sqlite3
from datetime import date, timedelta

SEED = 20260721

SCALE = int(os.getenv("DOGFOOD_SCALE", "1"))
N_PATIENTS = 900 * SCALE
N_ENCOUNTERS = 5_000 * SCALE
N_PRACTITIONERS = 140 * SCALE

# No REFERENCES clauses anywhere: the point is a schema that declares nothing.
DDL = """
CREATE TABLE region (
    region_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    country TEXT NOT NULL
);

CREATE TABLE practice (
    practice_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    region_id INTEGER,
    opened_on TEXT
);

CREATE TABLE patient (
    patient_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    sex TEXT,
    birth_year INTEGER,
    practice_id INTEGER,
    is_active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE practitioner (
    practitioner_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    speciality TEXT NOT NULL,
    practice_id INTEGER
);

CREATE TABLE encounter (
    encounter_id INTEGER PRIMARY KEY,
    patient_id INTEGER NOT NULL,
    practitioner_id INTEGER,
    encounter_date TEXT NOT NULL,
    kind TEXT NOT NULL,
    duration_minutes INTEGER,
    cost DECIMAL(10,2),
    adjustment DECIMAL(10,2)
);

CREATE TABLE diagnosis (
    diagnosis_id INTEGER PRIMARY KEY,
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    chronic INTEGER NOT NULL DEFAULT 0
);

-- Composite natural key, no surrogate id.
CREATE TABLE encounter_diagnosis (
    encounter_id INTEGER NOT NULL,
    diagnosis_id INTEGER NOT NULL,
    rank INTEGER NOT NULL,
    PRIMARY KEY (encounter_id, diagnosis_id)
);

CREATE TABLE observation (
    observation_id INTEGER PRIMARY KEY,
    encounter_id INTEGER NOT NULL,
    code TEXT NOT NULL,
    value_numeric REAL,
    unit TEXT
);

CREATE INDEX idx_enc_patient ON encounter(patient_id);
CREATE INDEX idx_obs_encounter ON observation(encounter_id);
"""

REGIONS = [("Northern", "UK"), ("Southern", "UK"), ("Midlands", "UK"),
           ("Highlands", "UK"), ("Coastal", "IE")]
SPECIALITIES = ["cardiology", "oncology", "paediatrics", "general practice",
                "orthopaedics", "dermatology"]
KINDS = ["routine", "urgent", "follow-up", "telehealth", "emergency"]
SEXES = ["female", "male", "other"]
DIAGNOSES = [("E11", "type 2 diabetes", 1), ("I10", "hypertension", 1),
             ("J45", "asthma", 1), ("M54", "back pain", 0),
             ("K21", "reflux", 0), ("F32", "depression", 1),
             ("L20", "eczema", 0), ("R51", "headache", 0)]
OBS_CODES = [("systolic", "mmHg"), ("diastolic", "mmHg"), ("hba1c", "mmol/mol"),
             ("weight", "kg"), ("height", "cm")]


def build(path: str) -> str:
    rng = random.Random(SEED)
    if os.path.exists(path):
        os.remove(path)
    conn = sqlite3.connect(path)
    conn.executescript(DDL)

    conn.executemany("INSERT INTO region VALUES (?,?,?)",
                     [(i + 1, n, c) for i, (n, c) in enumerate(REGIONS)])

    start = date(2021, 1, 1)
    n_practices = 40 * SCALE
    conn.executemany(
        "INSERT INTO practice VALUES (?,?,?,?)",
        [(i + 1, f"Practice {i + 1}", rng.randint(1, len(REGIONS)),
          (start + timedelta(days=rng.randint(0, 400))).isoformat())
         for i in range(n_practices)])

    conn.executemany(
        "INSERT INTO patient VALUES (?,?,?,?,?,?)",
        [(i + 1, f"Patient {i + 1}", rng.choice(SEXES),
          rng.randint(1930, 2020), rng.randint(1, n_practices),
          0 if i % 11 == 0 else 1) for i in range(N_PATIENTS)])

    conn.executemany(
        "INSERT INTO practitioner VALUES (?,?,?,?)",
        [(i + 1, f"Dr {i + 1}", rng.choice(SPECIALITIES),
          rng.randint(1, n_practices)) for i in range(N_PRACTITIONERS)])

    conn.executemany("INSERT INTO diagnosis VALUES (?,?,?,?)",
                     [(i + 1, c, n, ch) for i, (c, n, ch) in enumerate(DIAGNOSES)])

    encounters = []
    for i in range(N_ENCOUNTERS):
        encounters.append((
            i + 1,
            rng.randint(1, N_PATIENTS),
            None if i % 23 == 0 else rng.randint(1, N_PRACTITIONERS),
            (start + timedelta(days=rng.randint(0, 1400))).isoformat(),
            rng.choice(KINDS),
            rng.choice([10, 15, 20, 30, 45, 60]),
            round(rng.uniform(20, 4000), 2),
            # Legitimately negative and legitimately zero.
            round(rng.choice([0.0, 0.0, -rng.uniform(1, 200), rng.uniform(1, 50)]), 2),
        ))
    conn.executemany(
        "INSERT INTO encounter VALUES (?,?,?,?,?,?,?,?)", encounters)

    pairs = set()
    for eid in range(1, N_ENCOUNTERS + 1):
        for rank in range(1, rng.randint(1, 4)):
            pairs.add((eid, rng.randint(1, len(DIAGNOSES)), rank))
    seen = set()
    rows = []
    for eid, did, rank in sorted(pairs):
        if (eid, did) in seen:
            continue
        seen.add((eid, did))
        rows.append((eid, did, rank))
    conn.executemany("INSERT INTO encounter_diagnosis VALUES (?,?,?)", rows)

    observations, oid = [], 1
    for eid in range(1, N_ENCOUNTERS + 1):
        for _ in range(rng.randint(0, 3)):
            code, unit = rng.choice(OBS_CODES)
            observations.append((oid, eid, code,
                                 None if oid % 41 == 0 else round(rng.uniform(1, 200), 2),
                                 unit))
            oid += 1
    conn.executemany("INSERT INTO observation VALUES (?,?,?,?,?)", observations)

    conn.commit()
    conn.close()
    return path


def stats(path: str) -> dict:
    conn = sqlite3.connect(path)
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
    finally:
        conn.close()


if __name__ == "__main__":
    import sys
    target = sys.argv[1] if len(sys.argv) > 1 else "dogfood_hospital.db"
    build(target)
    for table, count in stats(target).items():
        print(f"{table:>22}  {count:>8,}")
