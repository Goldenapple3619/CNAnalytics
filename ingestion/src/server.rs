use std::sync::{
    Arc,
    atomic::{AtomicU64, Ordering},
};

use tokio::sync::mpsc;
use tonic::Status;

use crate::analytics::{AnalyticsEvent, EventError, IngestionResponse};
use crate::time::current_timestamp;

const MAX_REPORTED_ERRORS: usize = 100;

const MAX_EVENT_ID_LEN: usize = 256;
const MAX_EVENT_TYPE_LEN: usize = 128;
const MAX_PAYLOAD_LEN: usize = 1024 * 1024;

#[derive(Default)]
pub struct Metrics {
    pub total_received: AtomicU64,
    pub total_processed: AtomicU64,
    pub total_failed: AtomicU64,
    pub total_duplicates: AtomicU64,
    pub queue_depth: AtomicU64,
}

#[derive(Clone)]
pub struct IngestionServer {
    pub tx_worker: mpsc::Sender<AnalyticsEvent>,
    pub metrics: Arc<Metrics>,
}

#[derive(Default)]
pub struct RequestOutcome {
    pub received: u64,
    pub accepted: u64,
    pub failed: u64,
    pub errors: Vec<EventError>,
}

impl RequestOutcome {
    fn reject(&mut self, event_id: &str, code: &str, message: String) {
        self.failed += 1;
        if self.errors.len() < MAX_REPORTED_ERRORS {
            self.errors.push(EventError {
                event_id: event_id.to_string(),
                error_code: code.to_string(),
                error_message: message,
            });
        }
    }
}

impl IngestionServer {
    pub async fn accept_event(&self, event: AnalyticsEvent, outcome: &mut RequestOutcome) -> Result<(), Status> {
        self.metrics.total_received.fetch_add(1, Ordering::Relaxed);
        outcome.received += 1;

        if let Err((code, message)) = validate_event(&event) {
            self.metrics.total_failed.fetch_add(1, Ordering::Relaxed);
            outcome.reject(&event.event_id, code, message);
            return Ok(());
        }

        self.metrics.queue_depth.fetch_add(1, Ordering::Relaxed);

        if let Err(e) = self.tx_worker.send(event).await {
            self.metrics.queue_depth.fetch_sub(1, Ordering::Relaxed);
            eprintln!("Worker channel error: {}", e);
            return Err(Status::unavailable("Worker unavailable"));
        }

        outcome.accepted += 1;
        Ok(())
    }

    pub fn build_response(&self, outcome: RequestOutcome) -> IngestionResponse {
        let message = if outcome.failed == 0 {
            "Events accepted successfully".to_string()
        } else {
            format!(
                "{} events accepted, {} rejected",
                outcome.accepted, outcome.failed
            )
        };

        IngestionResponse {
            success: outcome.failed == 0,
            count_received: outcome.received,
            count_processed: outcome.accepted,
            count_failed: outcome.failed,
            // Duplicates are detected asynchronously by the SQLite writer
            // (UNIQUE event_id), after the response has been sent.
            count_duplicates: 0,
            errors: outcome.errors,
            message,
            server_timestamp: Some(current_timestamp()),
        }
    }
}

fn validate_event(event: &AnalyticsEvent) -> Result<(), (&'static str, String)> {
    if event.event_id.is_empty() {
        return Err(("MISSING_EVENT_ID", "event_id is required".into()));
    }
    if event.event_id.len() > MAX_EVENT_ID_LEN {
        return Err((
            "FIELD_TOO_LONG",
            format!("event_id exceeds {} bytes", MAX_EVENT_ID_LEN),
        ));
    }
    if event.event_type.is_empty() {
        return Err(("MISSING_EVENT_TYPE", "event_type is required".into()));
    }
    if event.event_type.len() > MAX_EVENT_TYPE_LEN {
        return Err((
            "FIELD_TOO_LONG",
            format!("event_type exceeds {} bytes", MAX_EVENT_TYPE_LEN),
        ));
    }
    if let Some(ts) = &event.timestamp
        && !(0..1_000_000_000).contains(&ts.nanos)
    {
        return Err(("INVALID_TIMESTAMP", "timestamp.nanos out of range".into()));
    }
    if !event.payload_json.is_empty() {
        if event.payload_json.len() > MAX_PAYLOAD_LEN {
            return Err((
                "FIELD_TOO_LONG",
                format!("payload_json exceeds {} bytes", MAX_PAYLOAD_LEN),
            ));
        }
        if let Err(e) = serde_json::from_str::<serde::de::IgnoredAny>(&event.payload_json) {
            return Err(("INVALID_PAYLOAD_JSON", format!("payload_json: {}", e)));
        }
    }
    Ok(())
}
