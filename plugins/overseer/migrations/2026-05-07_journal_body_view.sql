-- Slice 9 — read-only view joining overseer_journal to body / activity
-- context.
--
-- Per the overseer's design pushback: don't pollute overseer_journal
-- with derived fields. A view (or a separate materialized join table
-- if performance demands) is the right shape. The journal table stays
-- pristine; this view is what synthesis reads when it wants context
-- on what Tory's body / behavior looked like the day an entry was
-- written.
--
-- Adjacency rule: body_metrics row is keyed on the date Tory's
-- measurements completed (i.e., the date a sleep cycle ended). For
-- a journal entry on date D, body_metrics(date=D) is the right join —
-- it reflects D's preceding-night sleep + D's resp/SpO2/temp
-- accumulations. Don't try to fetch yesterday's row; the schema is
-- already aligned.

DROP VIEW IF EXISTS journal_with_body_context;

CREATE VIEW journal_with_body_context AS
SELECT
    j.id                              AS journal_id,
    j.written_at,
    substr(j.written_at, 1, 10)       AS journal_date,
    j.triggered_by,
    j.body                            AS journal_body,
    j.provisionality,
    j.referenced_artifacts,

    -- Body context (NULL when no Fitbit data for that date)
    bm.sleep_score,
    bm.deep_sleep_min,
    bm.sleep_resting_hr,
    bm.rmssd                          AS hrv_rmssd,
    bm.respiratory_rate,
    bm.spo2_avg,
    bm.wrist_temp_delta_avg,
    bm.active_minutes_total,
    bm.body_response_count,

    -- Activity context (per-service counts on that date)
    (SELECT event_count FROM activity_daily
     WHERE date = substr(j.written_at, 1, 10)
       AND service = 'search') AS search_events_today,
    (SELECT novelty_score FROM activity_daily
     WHERE date = substr(j.written_at, 1, 10)
       AND service = 'search') AS search_novelty_today,
    (SELECT event_count FROM activity_daily
     WHERE date = substr(j.written_at, 1, 10)
       AND service = 'youtube') AS youtube_events_today,
    (SELECT event_count FROM activity_daily
     WHERE date = substr(j.written_at, 1, 10)
       AND service = 'chrome') AS chrome_events_today,
    (SELECT event_count FROM activity_daily
     WHERE date = substr(j.written_at, 1, 10)
       AND service = 'gemini') AS gemini_events_today,

    -- Call context: total calls that day (NOT broken out by counterparty;
    -- counterparty queries happen in the synthesis layer when relational
    -- themes are being built, per the posture doc).
    (SELECT COUNT(*) FROM phone_calls
     WHERE substr(start_ts, 1, 10) = substr(j.written_at, 1, 10))
                                      AS calls_today,
    (SELECT SUM(duration_min) FROM phone_calls
     WHERE substr(start_ts, 1, 10) = substr(j.written_at, 1, 10))
                                      AS call_minutes_today

FROM overseer_journal j
LEFT JOIN body_metrics bm
       ON bm.date = substr(j.written_at, 1, 10);

-- Sanity check: when this view is queried for a journal entry on a
-- date with full Fitbit + activity data, every column should be
-- populated. NULLs on body_* fields mean "no Fitbit data that day"
-- (device not worn / not charged) — consult data_horizons before
-- inferring anything from absence.
