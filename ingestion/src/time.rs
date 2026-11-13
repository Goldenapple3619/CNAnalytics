use std::time::{SystemTime, UNIX_EPOCH};

pub fn current_timestamp() -> prost_types::Timestamp {
    let duration = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default();

    prost_types::Timestamp {
        seconds: duration.as_secs() as i64,
        nanos: duration.subsec_nanos() as i32,
    }
}

pub fn now_millis() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as i64
}

pub fn timestamp_to_millis(ts: &prost_types::Timestamp) -> i64 {
    ts.seconds
        .saturating_mul(1000)
        .saturating_add(i64::from(ts.nanos) / 1_000_000)
}
