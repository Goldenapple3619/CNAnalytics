mod server;
mod services;
mod time;

use std::path::PathBuf;
use std::sync::Arc;

use tokio::sync::mpsc;
use tonic::transport::Server;

use crate::analytics::AnalyticsEvent;
use crate::analytics::ingestion_service_server::IngestionServiceServer;
use crate::server::{IngestionServer, Metrics};
use crate::services::db::{setup_db_pool, spawn_sqlite_writer};
use crate::services::ingestion::start_background_worker;

pub mod analytics {
    tonic::include_proto!("analytics");
}

const HTTP2_WINDOW_SIZE: u32 = 8 * 1024 * 1024;

fn env_or(key: &str, default: &str) -> String {
    std::env::var(key).unwrap_or_else(|_| default.to_string())
}

async fn shutdown_signal() {
    #[cfg(unix)]
    {
        use tokio::signal::unix::{SignalKind, signal};
        let mut term = signal(SignalKind::terminate()).expect("failed to install SIGTERM handler");
        tokio::select! {
            _ = tokio::signal::ctrl_c() => {}
            _ = term.recv() => {}
        }
    }
    #[cfg(not(unix))]
    {
        let _ = tokio::signal::ctrl_c().await;
    }
    println!("Shutdown signal received, draining queues...");
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let addr = env_or("ANALYTICS_ADDR", "0.0.0.0:50051").parse()?;
    let queue_path = PathBuf::from(env_or("ANALYTICS_QUEUE_PATH", "../data/queue.dat"));
    let db_path = env_or("ANALYTICS_DB_PATH", "../data/db.sqlite");

    let metrics = Arc::new(Metrics::default());
    let pool = setup_db_pool(&db_path)?;

    let (tx, rx) = mpsc::channel::<AnalyticsEvent>(10_000);
    let (storage_tx, storage_rx) = mpsc::channel::<Vec<u8>>(64);

    let writer = spawn_sqlite_writer(pool, queue_path, storage_rx, metrics.clone())?;
    let worker = tokio::spawn(start_background_worker(rx, storage_tx, metrics.clone()));

    let ingestion_service = IngestionServer {
        tx_worker: tx,
        metrics,
    };

    println!("CNAnalytics gRPC server started on {}", addr);

    Server::builder()
        .initial_connection_window_size(HTTP2_WINDOW_SIZE)
        .initial_stream_window_size(HTTP2_WINDOW_SIZE)
        .add_service(IngestionServiceServer::new(ingestion_service))
        .serve_with_shutdown(addr, shutdown_signal())
        .await?;

    worker.await?;
    tokio::task::spawn_blocking(move || writer.join()).await?.ok();

    println!("Shutdown complete.");
    Ok(())
}
