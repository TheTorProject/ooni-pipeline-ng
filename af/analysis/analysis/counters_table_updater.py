"""
Updates `counters` table

Runs in a dedicated thread

"""

# TODO: regenerate data for the previous day once a day


import logging
import time

import psycopg2

from analysis.metrics import setup_metrics

log = logging.getLogger("analysis")
metrics = setup_metrics(name="analysis")


@metrics.timer("populate_counters_table")
def _populate_counters_table(cur):
    log.info("Populating counters table from historical data")
    sql = """
    INSERT INTO counters (measurement_start_day, test_name, probe_cc, probe_asn, input, anomaly, confirmed, failure, count)
    SELECT
        date_trunc('day', measurement_start_time) AS measurement_start_day,
        test_name::text,
        probe_cc,
        probe_asn,
        input,
        anomaly,
        confirmed,
        measurement.exc IS NOT NULL AS failure,
        COUNT(*) AS count
    FROM
        measurement
        JOIN input ON input.input_no = measurement.input_no
        JOIN report ON report.report_no = measurement.report_no
    WHERE
        measurement_start_time < CURRENT_DATE
    GROUP BY
        measurement_start_day,
        test_name,
        probe_cc,
        probe_asn,
        input,
        anomaly,
        confirmed,
        failure;
    """
    cur.execute(sql)
    log.info("Populated with %d rows", cur.rowcount)


def _table_is_empty(cur):
    cur.execute("SELECT input FROM counters LIMIT 1")
    result = cur.fetchone()
    return len(result) == 0


def connect_db(c):
    return psycopg2.connect(
        dbname=c["dbname"], user=c["dbuser"], host=c["dbhost"], password=c["dbpassword"]
    )


@metrics.timer("update_counters_table")
def _update_counters_table(conf):
    log.info("Started update_counters_table thread")
    conn = connect_db(conf.active)
    cur = conn.cursor()
    if _table_is_empty(cur):
        _populate_counters_table(cur)

    log.info("Deleting today's data")
    sql = "DELETE FROM counters WHERE measurement_start_day = CURRENT_DATE"
    cur.execute(sql)
    log.info("Deleted: %d", cur.rowcount)

    log.info("Regenerating today's data")
    sql = """
    INSERT INTO counters (measurement_start_day, test_name, probe_cc, probe_asn, input, anomaly, confirmed, failure, count)
    SELECT
        date_trunc('day', measurement_start_time) AS measurement_start_day,
        test_name::text,
        probe_cc,
        probe_asn,
        input,
        anomaly,
        confirmed,
        msm_failure AS failure,
        COUNT(*) AS count
    FROM
        fastpath
    WHERE
        date_trunc('day', measurement_start_time) = CURRENT_DATE
    GROUP BY
        measurement_start_day,
        test_name,
        probe_cc,
        probe_asn,
        input,
        anomaly,
        confirmed,
        failure;
    """
    cur.execute(sql)
    log.info("Inserted: %d", cur.rowcount)

    conn.commit()
    conn.close()
    log.info("Done")


def counters_table_updater(conf):
    """Thread entry point
    """
    while True:
        try:
            _update_counters_table(conf)
        except Exception as e:
            log.exception(e)

        time.sleep(3600)


# Manual run
def main():
    from argparse import Namespace

    logging.basicConfig(level=logging.DEBUG)
    c = Namespace(
        active=dict(
            dbname="metadb", dbuser="shovel", dbhost="localhost", dbpassword=None
        )
    )
    _update_counters_table(c)
