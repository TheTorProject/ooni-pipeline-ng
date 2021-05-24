"""
Updates counters* tables at frequent intervals:
 - counters
 - counters_asn_noinput
 - counters_noinput

Update global and country stats materialized views every day.

Runs in a dedicated thread

"""

# TODO: regenerate data for the previous day once a day

from datetime import datetime, timedelta

import logging

import psycopg2

from analysis.metrics import setup_metrics

log = logging.getLogger("analysis")
metrics = setup_metrics(name="counters_update")


@metrics.timer("populate_counters_table")
def _populate_counters_table(cur):
    # Used once
    log.info("Populating counters table from historical data")
    sql = """
    INSERT INTO counters
    SELECT
        date_trunc('day', measurement_start_time) AS measurement_start_day,
        test_name::text,
        probe_cc,
        probe_asn,
        input,
        count(CASE WHEN anomaly THEN 1 END) AS anomaly_count,
        count(CASE WHEN confirmed THEN 1 END) AS confirmed_count,
        count(CASE WHEN msm_failure THEN 1 END) AS failure_count,
        COUNT(*) AS measurement_count
    FROM fastpath
    GROUP BY
        measurement_start_day,
        test_name,
        probe_cc,
        probe_asn,
        input
    ON CONFLICT (measurement_start_day, test_name, probe_cc, probe_asn, input)
    DO nothing
    """
    cur.execute(sql)
    log.info("Populated with %d rows", cur.rowcount)


def _table_is_empty(cur):
    cur.execute("SELECT input FROM counters LIMIT 1")
    result = cur.fetchone()
    return result is None or len(result) == 0


def connect_db(c):
    return psycopg2.connect(
        dbname=c["dbname"], user=c["dbuser"], host=c["dbhost"], password=c["dbpassword"]
    )


# # Update counters tables using upsert # #


def query(metric, cur, sql, **kw):
    """Run and log query"""
    log.info("Running: %s %s", sql, kw)
    cur.execute(sql, kw)
    log.info("Inserted: %d", cur.rowcount)
    metrics.gauge("update_counters_table.rowcount", cur.rowcount)


@metrics.timer("update_counters_hourly_software_table")
def update_counters_hourly_software_table(conn, msm_uid_start, msm_uid_end):
    # transaction, commit on context exiting
    sql = """
    INSERT INTO counters_hourly_software
    SELECT
        date_trunc('hour', measurement_start_time) AS measurement_start_hour,
        platform,
        software_name,
        COUNT(*) AS measurement_count
    FROM
        fastpath
    WHERE measurement_uid > %(msm_uid_start)s
    AND measurement_uid < %(msm_uid_end)s
    GROUP BY
        measurement_start_hour,
        platform,
        software_name
    ON CONFLICT (measurement_start_hour, platform, software_name)
    DO UPDATE SET
            measurement_count = counters_hourly_software.measurement_count + EXCLUDED.measurement_count;
    """
    with conn:
        log.info("Upserting into counters_hourly_software table")
        cur = conn.cursor()
        query(
            "update_counters_hourly_software_table.rowcount",
            cur,
            sql,
            msm_uid_start=msm_uid_start,
            msm_uid_end=msm_uid_end,
        )


@metrics.timer("update_counters_table")
def update_counters_table(conn, msm_uid_start, msm_uid_end):
    # transaction, commit on context exiting
    with conn:
        log.info("Upserting into counters table")
        sql = """
    INSERT INTO counters
    SELECT
        date_trunc('day', measurement_start_time) AS measurement_start_day,
        test_name::text,
        probe_cc,
        probe_asn,
        input,
        count(CASE WHEN anomaly THEN 1 END) AS anomaly_count,
        count(CASE WHEN confirmed THEN 1 END) AS confirmed_count,
        count(CASE WHEN msm_failure THEN 1 END) AS failure_count,
        COUNT(*) AS measurement_count
    FROM
        fastpath
    WHERE measurement_uid > %(msm_uid_start)s
    AND measurement_uid < %(msm_uid_end)s
    GROUP BY
        measurement_start_day,
        test_name,
        probe_cc,
        probe_asn,
        input
    ON CONFLICT (measurement_start_day, test_name, probe_cc, probe_asn, input)
    DO UPDATE
    SET anomaly_count = counters.anomaly_count + EXCLUDED.anomaly_count,
    confirmed_count  =  counters.confirmed_count + EXCLUDED.confirmed_count,
    failure_count  = counters.failure_count + EXCLUDED.failure_count,
    measurement_count  = counters.measurement_count + EXCLUDED.measurement_count
    """
        cur = conn.cursor()
        query(
            "update_counters_table.rowcount",
            cur,
            sql,
            msm_uid_start=msm_uid_start,
            msm_uid_end=msm_uid_end,
        )


def create_counters_test_list_matview(conn):
    # transaction, commit on context exiting
    sql = """
        CREATE MATERIALIZED VIEW IF NOT EXISTS counters_test_list AS
        SELECT probe_cc, input, SUM(measurement_count) AS msmt_cnt
        FROM counters
        WHERE measurement_start_day < CURRENT_DATE + interval '1 days'
        AND measurement_start_day > CURRENT_DATE - interval '8 days'
        AND test_name = 'web_connectivity'
        GROUP BY 1, 2
        WITH NO DATA;
        CREATE INDEX IF NOT EXISTS counters_test_list_idx
        ON counters_test_list USING btree (probe_cc);
    """
    with conn:
        log.info("Create counters_test_list table")
        cur = conn.cursor()
        query("create_counters_test_list_matview.rowcount", cur, sql)


@metrics.timer("refresh_counters_test_list_matview")
def refresh_counters_test_list_matview(conn):
    # transaction, commit on context exiting
    with conn:
        log.info("Refresh counters_test_list table")
        sql = "REFRESH MATERIALIZED VIEW counters_test_list"
        cur = conn.cursor()
        query("refresh_counters_test_list_matview.rowcount", cur, sql)


@metrics.timer("update_counters_asn_noinput_table")
def update_counters_asn_noinput_table(conn, msm_uid_start, msm_uid_end):
    # transaction, commit on context exiting
    with conn:
        log.info("Upserting into counters_asn_noinput table")
        sql = """
    INSERT INTO counters_asn_noinput
    SELECT
        date_trunc('day', measurement_start_time) AS measurement_start_day,
        test_name::text,
        probe_cc,
        probe_asn,
        count(CASE WHEN anomaly THEN 1 END) AS anomaly_count,
        count(CASE WHEN confirmed THEN 1 END) AS confirmed_count,
        count(CASE WHEN msm_failure THEN 1 END) AS failure_count,
        COUNT(*) AS measurement_count
    FROM
        fastpath
    WHERE measurement_uid > %(msm_uid_start)s
    AND measurement_uid < %(msm_uid_end)s
    GROUP BY
        measurement_start_day,
        test_name,
        probe_cc,
        probe_asn
    ON CONFLICT (measurement_start_day, test_name, probe_cc, probe_asn)
    DO UPDATE
    SET anomaly_count = counters_asn_noinput.anomaly_count + EXCLUDED.anomaly_count,
    confirmed_count  =  counters_asn_noinput.confirmed_count + EXCLUDED.confirmed_count,
    failure_count  = counters_asn_noinput.failure_count + EXCLUDED.failure_count,
    measurement_count  = counters_asn_noinput.measurement_count + EXCLUDED.measurement_count
    """
        cur = conn.cursor()
        query(
            "update_counters_asn_noinput_table.rowcount",
            cur,
            sql,
            msm_uid_start=msm_uid_start,
            msm_uid_end=msm_uid_end,
        )


@metrics.timer("update_counters_noinput_table")
def update_counters_noinput_table(conn, msm_uid_start, msm_uid_end):
    # transaction, commit on context exiting
    with conn:
        log.info("Upserting into counters_noinput table")
        sql = """
    INSERT INTO counters_noinput
    SELECT
        date_trunc('day', measurement_start_time) AS measurement_start_day,
        test_name::text,
        probe_cc,
        count(CASE WHEN anomaly THEN 1 END) AS anomaly_count,
        count(CASE WHEN confirmed THEN 1 END) AS confirmed_count,
        count(CASE WHEN msm_failure THEN 1 END) AS failure_count,
        COUNT(*) AS measurement_count
    FROM
        fastpath
    WHERE measurement_uid > %(msm_uid_start)s
    AND measurement_uid < %(msm_uid_end)s
    GROUP BY
        measurement_start_day,
        test_name,
        probe_cc
    ON CONFLICT (measurement_start_day, test_name, probe_cc)
    DO UPDATE
    SET anomaly_count = counters_noinput.anomaly_count + EXCLUDED.anomaly_count,
    confirmed_count =  counters_noinput.confirmed_count + EXCLUDED.confirmed_count,
    failure_count = counters_noinput.failure_count + EXCLUDED.failure_count,
    measurement_count  = counters_noinput.measurement_count + EXCLUDED.measurement_count
    """
        cur = conn.cursor()
        query(
            "update_counters_noinput_table.rowcount",
            cur,
            sql,
            msm_uid_start=msm_uid_start,
            msm_uid_end=msm_uid_end,
        )


@metrics.timer("update_all_counters_tables")
def update_all_counters_tables(conf):
    """Update counters tables using upsert
    Allows for very fast updates by filtering on measurement_uid
    Even if we receive an "old" msmt days after the tests, we still update the
    correct counter.
    WARNING: the update interval must match the systemd timer
    WARNING: the upsert is doing a sum and creates false data if run at the
    wrong times
    If we miss or duplicate a run the only option is to truncate/recreate
    the tables completely or partially
    """
    log.info("Started update_all_counters_tables")
    metrics.gauge("update_all_counters_tables.running", 1)

    fn = conf.output_directory / "counters_table_updater.last_msm_uid_end"
    try:
        msm_uid_start = fn.read_text().strip()
    except FileNotFoundError:
        log.warn("%s not found, defaulting to utcnow", fn)
        msm_uid_start = datetime.utcnow().strftime("%Y%m%d%H%M")

    end = datetime.utcnow() - timedelta(minutes=10)
    msm_uid_end = end.strftime("%Y%m%d%H%M")
    fn.write_text(msm_uid_end)

    conn = connect_db(conf.active)
    # transaction, commit on context exiting
    with conn:
        update_counters_hourly_software_table(conn, msm_uid_start, msm_uid_end)

    with conn:
        update_counters_table(conn, msm_uid_start, msm_uid_end)
        create_counters_test_list_matview(conn)
        refresh_counters_test_list_matview(conn)

    with conn:
        update_counters_asn_noinput_table(conn, msm_uid_start, msm_uid_end)

    with conn:
        update_counters_noinput_table(conn, msm_uid_start, msm_uid_end)

    conn.close()
    metrics.gauge("update_all_counters_tables.running", 0)
    log.info("Done")


# # Update materialized view tables once a day # #


@metrics.timer("refresh_global_stats")
def refresh_global_stats(conn):
    log.info("Running refresh_global_stats")
    with conn.cursor() as cur:
        sql = "REFRESH MATERIALIZED VIEW global_stats"
        cur.execute(sql)
        log.info("Populated with %d rows", cur.rowcount)


@metrics.timer("refresh_country_stats")
def refresh_country_stats(conn):
    log.info("Running refresh_country_stats")
    with conn.cursor() as cur:
        sql = "REFRESH MATERIALIZED VIEW country_stats"
        cur.execute(sql)


@metrics.timer("refresh_global_by_month")
def refresh_global_by_month(conn):
    log.info("Running refresh_global_by_month")
    with conn.cursor() as cur:
        sql = "REFRESH MATERIALIZED VIEW global_by_month"
        cur.execute(sql)


@metrics.timer("update_tables_daily")
def update_tables_daily(conf):
    """Refresh materialized view tables
    Takes 10 to 20 minutes

    The tables are created in database_upgrade_schema.py
    """
    log.info("Started update_tables_daily")
    metrics.gauge("update_tables_daily.running", 1)
    conn = connect_db(conf.active)
    with conn:
        refresh_global_stats(conn)

    with conn:
        refresh_country_stats(conn)

    with conn:
        refresh_global_by_month(conn)

    conn.close()
    metrics.gauge("update_tables_daily.running", 0)
    log.info("Done")
