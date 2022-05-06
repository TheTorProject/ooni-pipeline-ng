#!/usr/bin/env python3
"""
A hybrid between fastpath and the ooni_api_uploader.py from the API
To be used once, fetches legacy cans from the legacy S3 bucket
Generates new postcans and jsonl files and uploads to the new S3 bucket
Updates both the fastpath and jsonl tables

Inputs:
  Legacy raw cans in old S3 bucket

Outputs:
  jsonl files in new S3 bucket e.g.:
    jsonl/{testname}/{cc}/{ts}/00/{jsonlf.name}
  rows in the jsonl database table
  rows in the fastpath database table

Usage:
export AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY
PYTHONPATH=. ./fastpath/reprocessor.py ooni-data ooni-data-eu-fra-test --day 2015-1-1

Note: the bundling of measurements into jsonl gz files has to remain deterministic

DB update:

BEGIN;
ALTER TABLE jsonl ADD COLUMN measurement_uid TEXT;
COMMIT;
CREATE UNIQUE INDEX CONCURRENTLY jsonl_unique ON jsonl (report_id, input, measurement_uid);
DROP INDEX IF EXISTS jsonl_lookup_idx;
ALTER TABLE jsonl DROP COLUMN id;

CREATE INDEX CONCURRENTLY jsonl_measurement_uid ON jsonl (measurement_uid);
"""

from argparse import ArgumentParser
from dataclasses import dataclass
from datetime import datetime, timedelta
from os import getenv
from pathlib import Path
import gzip
import hashlib
import logging
import os
import time

import json
import psycopg2  # debdeps: python3-psycopg2
from psycopg2.extras import execute_values
import statsd  # debdeps: python3-statsd

import fastpath.db as db
import fastpath.s3feeder as s3f
from fastpath.db import extract_input_domain
from fastpath.core import score_measurement, setup_fingerprints, unwrap_msmt

metrics = statsd.StatsClient("127.0.0.1", 8125, prefix="reprocessor")
log = logging.getLogger("reprocessor")
log.addHandler(logging.StreamHandler())  # Writes to console
log.setLevel(logging.DEBUG)
for lo in ("urllib3", "botocore", "s3transfer", "boto"):
    logging.getLogger(lo).setLevel(logging.INFO)

import boto3
import botocore.exceptions

stats = dict(files_uploaded=0, files_size_mismatch=0, files_generated=0, t0=0)


def create_s3_client(conf):
    session = boto3.Session(
        aws_access_key_id=getenv("aws_access_key_id"),
        aws_secret_access_key=getenv("aws_secret_access_key"),
    )
    return session.resource("s3")


def update_db_table(conn, lookup_list, jsonl_mode):
    if jsonl_mode == "dryrun":
        return

    q = """INSERT INTO jsonl
    (report_id, input, measurement_uid, s3path, linenum) VALUES %s
    ON CONFLICT (report_id, input, measurement_uid) DO
    """
    if jsonl_mode == "upsert":
        log.info(f"Upserting {len(lookup_list)} rows to jsonl table")
        q += "UPDATE SET s3path = excluded.s3path, linenum = excluded.linenum"
    elif jsonl_mode == "insert":
        log.info(f"Inserting {len(lookup_list)} rows to jsonl table")
        q += "NOTHING"
    else:
        raise Exception(f"Unexpected --jsonlmode value {jsonl_mode}")

    with conn.cursor() as cur:
        execute_values(cur, q, lookup_list)
        conn.commit()


def update_jsonl_clickhouse_table(conn, lookup_list, jsonl_mode):
    if jsonl_mode == "dryrun":
        return

    # FIXME table name
    q = """INSERT INTO new_jsonl
    (report_id, input, measurement_uid, s3path, linenum, date, source) VALUES
    """
    x = conn.execute(q, lookup_list)
    log.info(f"Inserted {x}")


@metrics.timer("upload_measurement")
def upload_to_s3(s3, bucket_name: str, tarf: Path, s3path: str) -> None:
    obj = s3.Object(bucket_name, s3path)
    log.info(f"Uploading {tarf} to {s3path}")
    obj.put(Body=tarf.read_bytes())
    stats["files_uploaded"] += 1


def s3_check(s3, bucket_name, local_file, s3path) -> str:
    """Checks if the file is present and has the same size"""
    try:
        obj = s3.Object(bucket_name, s3path)
        size = obj.content_length
    except botocore.exceptions.ClientError as e:
        if e.response["Error"]["Code"] == "404":
            log.info(f"file not found on S3 {bucket_name} {s3path}")
            return "not-found"
        raise

    disk_size = local_file.stat().st_size
    if disk_size != size:
        log.info(f"Size difference: {size} {disk_size} {size - disk_size}")
        stats["files_size_mismatch"] += 1
        return "different"

    log.info("File found")
    return "same"


def parse_date(d):
    return datetime.strptime(d, "%Y-%m-%d").date()


def parse_args():
    os.environ["TZ"] = "UTC"
    ap = ArgumentParser(__doc__)
    ap.add_argument("src_bucket")
    ap.add_argument("dst_bucket")
    ap.add_argument("--day", type=lambda d: parse_date(d))
    ap.add_argument(
        "--jsonlmode",
        choices=["dryrun", "insert", "upsert"],
        default="dryrun",
        help="jsonl table update policy. insert = never overwrite",
    )
    ap.add_argument(
        "--fastpathmode",
        choices=["dryrun", "insert", "upsert"],
        default="dryrun",
        help="fastpath table update policy. insert = never overwrite",
    )
    ap.add_argument(
        "--s3mode",
        choices=["check", "create", "dryrun", "create-if-needed"],
        default="check",
        help="create jsonl files on S3 or just check",
    )
    ap.add_argument("--db-uri")
    ap.add_argument("--clickhouse-url")
    c = ap.parse_args()

    return c


def score_measurement_and_upsert_fastpath(msm, msmt_uid, do_update: bool) -> None:
    scores = score_measurement(msm)
    anomaly = scores.get("blocking_general", 0.0) > 0.5
    failure = scores.get("accuracy", 1.0) < 0.5
    confirmed = scores.get("confirmed", False)

    sw_name = msm.get("software_name", "unknown")
    sw_version = msm.get("software_version", "unknown")
    platform = "unset"
    if "annotations" in msm and isinstance(msm["annotations"], dict):
        platform = msm["annotations"].get("platform", "unset")

    db.clickhouse_upsert_summary(
        msm,
        scores,
        anomaly,
        confirmed,
        failure,
        msmt_uid,
        sw_name,
        sw_version,
        platform,
    )


@dataclass
class Entity:
    jsonlf: Path
    fd: gzip.GzipFile
    jsonl_s3path_base: str
    lookup_list: list


@metrics.timer("finalize_jsonl")
def finalize_jsonl(s3sig, db_conn, conf, e: Entity) -> None:
    """For each JSONL file we do one upload to S3 and one
    INSERT query with many rows
    """
    jsize = int(e.fd.offset / 1024)
    log.info(f"Closing and preparing {e.jsonlf} Size: {jsize} KB")
    e.fd.close()

    # Calculate unique hash
    # update e.lookup_list
    # change e.jsonl_s3path
    hasher = hashlib.shake_128()
    for table_row in e.lookup_list:
        rid = table_row[2]
        hasher.update(rid.encode())
    h = hasher.hexdigest(8)
    jsonl_s3path = e.jsonl_s3path_base + f"{h}.jsonl.gz"
    log.info(f"Uploading {e.jsonlf} as {jsonl_s3path}")

    for n, table_row in enumerate(e.lookup_list):
        e.lookup_list[n][3] = jsonl_s3path

    stats["files_generated"] += 1
    if conf.s3mode == "create":
        upload_to_s3(s3sig, conf.dst_bucket, e.jsonlf, jsonl_s3path)
    elif conf.s3mode == "check":
        s3_check(s3sig, conf.dst_bucket, e.jsonlf, jsonl_s3path)
    elif conf.s3mode == "create-if-needed":
        s3status = s3_check(s3sig, conf.dst_bucket, e.jsonlf, jsonl_s3path)
        if s3status == "not-found":
            upload_to_s3(s3sig, conf.dst_bucket, e.jsonlf, jsonl_s3path)

    # update_db_table(db_conn, e.lookup_list, conf.jsonlmode)
    update_jsonl_clickhouse_table(db_conn, e.lookup_list, conf.jsonlmode)
    e.jsonlf.unlink()


@metrics.timer("process_measurement")
def process_measurement(can_fn, msm_tup, buf, seen_uids, conf, s3sig, db_conn):
    """Process a msmt
    If needed: create a new Entity tracking a jsonl file,
      close and upload jsonl to S3 and upsert db
    """
    THRESHOLD = 20 * 1024 * 1024
    msm_jstr, msm, msmt_uid = msm_tup
    if msm is None:
        msm = json.loads(msm_jstr)
    try:
        if sorted(msm.keys()) == ["content", "format"]:
            msm = unwrap_msmt(msm)

        rid = msm.get("report_id")
        test_name = msm.get("test_name")
        input_, domain = extract_input_domain(msm, test_name)
        assert not isinstance(input_, list)
        tn = test_name.replace("_", "")
        cc = msm.get("probe_cc").upper()
        desc = f"{msmt_uid} {tn} {cc} {rid} {input_}"
    except Exception as e:
        log.info(f"Ignoring broken measurement {rid}")
        metrics.incr("broken_measurement")
        return

    if msm.get("report_id") is None:
        log.debug(f"Ignoring measurement without report_id")
        metrics.incr("discarded_measurement")
        return

    if msm.get("probe_cc", "").upper() == "ZZ":
        log.debug(f"Ignoring measurement with probe_cc=ZZ {desc}")
        metrics.incr("discarded_measurement")
        return

    if msm.get("probe_asn", "").upper() == "AS0":
        log.debug(f"Ignoring measurement with ASN 0  {desc}")
        metrics.incr("discarded_measurement")
        return

    if msmt_uid in seen_uids:
        log.info(f"Ignoring DUPLICATE {desc}")
        metrics.incr("duplicate_measurement")
        return

    if not msm.get("measurement_start_time"):
        log.debug("Ignoring measurement with invalid measurement_start_time")
        metrics.incr("discarded_measurement")
        return

    log.debug(f"Processing {desc}")
    seen_uids.add(msmt_uid)

    # cc tn -> [entity1, entity2, ... ]

    entities = buf.setdefault(f"{cc} {tn}", [])
    if len(entities) == 0 or entities[-1].fd.closed:
        ts = conf.day.strftime("%Y%m%d")
        jsonlf = Path(f"{ts}_{cc}_{tn}.l.{len(entities)}.jsonl.gz")
        jsonl_s3path_base = f"jsonl/{tn}/{cc}/{ts}/00/{ts}_{cc}_{tn}.x."
        # An Entity is a JSONL file [that will be uploaded] on S3
        en = Entity(
            jsonlf=jsonlf,
            fd=gzip.open(jsonlf, "w"),
            jsonl_s3path_base=jsonl_s3path_base,
            lookup_list=[],
        )
        entities.append(en)

    en = entities[-1]

    # Add msmt to open jsonl file
    try:
        jmsm = json.dumps(msm)
    except TypeError as e:
        log.error(e, exc_info=True)
        log.error(msm)
        raise

    en.fd.write(jmsm.encode())
    en.fd.write(b"\n")

    rid = msm.get("report_id") or ""  # type: str
    source = can_fn
    try:
        assert can_fn.startswith("canned/20")
        date = parse_date(can_fn.split("/", 2)[1])
    except Exception:
        log.error(f"Unable to extract date from {can_fn}")
        date = None

    # report_id, input, measurement_uid, s3path, linenum, date, source
    i = [rid, input_, msmt_uid, None, len(en.lookup_list), date, source]
    en.lookup_list.append(i)

    if en.fd.offset > THRESHOLD:
        # The jsonlf is big enough
        finalize_jsonl(s3sig, db_conn, conf, en)

    if conf.fastpathmode in ("insert", "upsert"):
        update = conf.fastpathmode == "upsert"
        score_measurement_and_upsert_fastpath(msm, msmt_uid, update)


def progress(t0, processed_size, tot_size):
    now = time.time()
    p = processed_size / tot_size
    if p == 0:
        return
    rem = (now - t0) / p - (now - t0)
    rem = str(timedelta(seconds=rem))
    log.info(f"Processed percentage: {100 * p} Remaining time: {rem} {stats}")


@metrics.timer("process_can")
def process_can(db_conn, s3uns, s3sig, can_fn, can_size, conf, buf, seen_uids):
    Path(can_fn).parent.mkdir(parents=True, exist_ok=True)
    log.info(f"Fetching can {can_fn}")
    s3uns.download_file(conf.src_bucket, can_fn, can_fn)
    for msm_tup in s3f.load_multiple(can_fn):
        process_measurement(can_fn, msm_tup, buf, seen_uids, conf, s3sig, db_conn)
    Path(can_fn).unlink()


@metrics.timer("total_run_time")
def main():
    conf = parse_args()
    log.info(f"From bucket {conf.src_bucket} to {conf.dst_bucket}")
    s3sig = create_s3_client(conf)  # signed client for writing
    if conf.db_uri:
        log.info(f"Connecting to PG at {conf.db_uri}")
        db_conn = psycopg2.connect(conf.db_uri)
        db.setup(conf)  # setup db conn inside db module
    elif conf.clickhouse_url:
        log.info(f"Connecting to CH at {conf.clickhouse_url}")
        db.setup_clickhouse(conf)
        db_conn = db.click_client
    setup_fingerprints()

    # s3_check(s3sig, "ooni-data-eu-fra", "none", "jsonl/tor/VE/20200827/00/20200827_VE_tor.l.0.jsonl.gz")
    # Fetch msmts for one day
    buf = {}  # "<cc> <testname>" -> jsonlf / fd / jsonl_s3path
    seen_uids = set()  # Avoid uploading duplicates

    # raw/20210601/00/SA/webconnectivity/2021060100_SA_webconnectivity.n0.0.jsonl.gz
    # jsonl_s3path = f"raw/{ts}/00/{cc}/{testname}/{jsonlf.name}"

    s3uns = s3f.create_s3_client()  # unsigned client for reading
    cans_fns = s3f.list_cans_on_s3_for_a_day(s3uns, conf.day)
    cans_fns = sorted(cans_fns)  # this is not enough to sort by time
    # cans_fns = [ x for x in cans_fns if "meek" in x[0] ] # FIXME
    tot_size = sum(size for _, size in cans_fns)
    # Reminder: listing and bundling of msmts has to remain deterministic
    processed_size = 0
    log.info(f"{tot_size/1024/1024/1024} GB to process")
    log.info(f"{len(cans_fns)} cans to process")
    stats["t0"] = time.time()
    #  TODO make assertions on msmt
    #  TODO add consistency check on trivial id found in fastpath table
    for can in cans_fns:
        can_fn, size = can
        process_can(db_conn, s3uns, s3sig, can_fn, size, conf, buf, seen_uids)
        processed_size += size
        progress(stats["t0"], processed_size, tot_size)

    log.info("Finish jsonl files still open")
    for json_entities in buf.values():
        for e in json_entities:
            if e.fd.closed:
                continue
            finalize_jsonl(s3sig, db_conn, conf, e)

    log.info("Exiting")


if __name__ == "__main__":
    main()
