use std::sync::Arc;
use std::sync::atomic::Ordering;

use tokio::sync::mpsc;
use tonic::{Request, Response, Status};

use crate::analytics::ingestion_service_server::IngestionService;
use crate::analytics::{
    AnalyticsBatch, AnalyticsEvent, IngestionResponse, StatusRequest, StatusResponse,
};
use crate::server::{IngestionServer, Metrics, RequestOutcome};
use crate::services::db::encode_chunk;
use crate::time::{current_timestamp, now_millis};

const MAX_CHUNK_EVENTS: usize = 500;

#[tonic::async_trait]
impl IngestionService for IngestionServer {
    async fn ingest_stream(
        &self,
        request: Request<tonic::Streaming<AnalyticsEvent>>,
    ) -> Result<Response<IngestionResponse>, Status> {
        let mut stream = request.into_inner();
        let mut outcome = RequestOutcome::default();

        while let Some(event) = stream.message().await? {
            self.accept_event(event, &mut outcome).await?;
        }

        println!(
            "Received {} individual events ({} rejected)",
            outcome.received, outcome.failed
        );

        Ok(Response::new(self.build_response(outcome)))
    }

    async fn ingest_batches(
        &self,
        request: Request<tonic::Streaming<AnalyticsBatch>>,
    ) -> Result<Response<IngestionResponse>, Status> {
        let mut stream = request.into_inner();
        let mut outcome = RequestOutcome::default();
        let mut batches: u64 = 0;

        while let Some(batch) = stream.message().await? {
            batches += 1;

            for event in batch.events {
                self.accept_event(event, &mut outcome).await?;
            }
        }

        println!(
            "Received {} events in {} batches ({} rejected)",
            outcome.received, batches, outcome.failed
        );

        Ok(Response::new(self.build_response(outcome)))
    }

    async fn get_status(
        &self,
        _request: Request<StatusRequest>,
    ) -> Result<Response<StatusResponse>, Status> {
        let m = &self.metrics;

        Ok(Response::new(StatusResponse {
            // Unhealthy once the background worker has stopped.
            healthy: !self.tx_worker.is_closed(),
            service_name: "CNAnalytics Ingestion".to_string(),
            version: "0.1.0".to_string(),
            total_received: m.total_received.load(Ordering::Relaxed),
            total_processed: m.total_processed.load(Ordering::Relaxed),
            total_failed: m.total_failed.load(Ordering::Relaxed),
            queue_size: m.queue_depth.load(Ordering::Relaxed),
            server_timestamp: Some(current_timestamp()),
        }))
    }
}

pub async fn start_background_worker(
    mut rx: mpsc::Receiver<AnalyticsEvent>,
    storage_tx: mpsc::Sender<Vec<u8>>,
    metrics: Arc<Metrics>,
) {
    println!("Background worker started...");

    let mut buf: Vec<AnalyticsEvent> = Vec::with_capacity(MAX_CHUNK_EVENTS);

    while rx.recv_many(&mut buf, MAX_CHUNK_EVENTS).await > 0 {
        let count = buf.len() as u64;

        let result = match encode_chunk(&buf, now_millis()) {
            Ok(chunk) => storage_tx
                .send(chunk)
                .await
                .map_err(|_| anyhow::anyhow!("SQLite writer is no longer running")),
            Err(e) => Err(e),
        };
        buf.clear();

        if let Err(e) = result {
            eprintln!("Worker failed to forward {} events: {:#}", count, e);
            metrics.total_failed.fetch_add(count, Ordering::Relaxed);
            metrics.queue_depth.fetch_sub(count, Ordering::Relaxed);

            if storage_tx.is_closed() {
                break;
            }
        }
    }

    println!("Background worker stopped.");
}
