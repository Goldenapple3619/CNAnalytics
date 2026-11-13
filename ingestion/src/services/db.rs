use std::collections::BTreeMap;
use std::path::PathBuf;
use std::sync::Arc;
use std::sync::atomic::Ordering;
use std::time::Duration;

use anyhow::Context;
use queue_file::QueueFile;
use r2d2::Pool;
use r2d2_sqlite::SqliteConnectionManager;
use rusqlite::params;
use serde::{Deserialize, Serialize};
use tokio::sync::mpsc::{self, error::TryRecvError};

use crate::analytics::AnalyticsEvent;
use crate::server::Metrics;
use crate::time::timestamp_to_millis;

pub type DbPool = Pool<SqliteConnectionManager>;

const MAX_ELEMS_PER_TX: usize = 100;
const MAX_INTAKE_CHUNKS: usize = 256;
const ZSTD_LEVEL: i32 = 3;
const RETRY_DELAY: Duration = Duration::from_secs(1);
const QUEUE_ADD_ATTEMPTS: u32 = 5;

const SCHEMA: &str = "
    CREATE TABLE IF NOT EXISTS analytics (
        id             INTEGER PRIMARY KEY,
        event_id       TEXT    NOT NULL UNIQUE,
        event_type     TEXT    NOT NULL,
        timestamp_ms   INTEGER NOT NULL,
        user_id        TEXT    NOT NULL,
        session_id     TEXT    NOT NULL,
        source         TEXT    NOT NULL,
        application    TEXT    NOT NULL,
        environment    TEXT    NOT NULL,
        schema_version INTEGER NOT NULL,
        payload_json   TEXT    NOT NULL,
        attributes     TEXT    NOT NULL,
        received_at_ms INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_analytics_type_ts ON analytics (event_type, timestamp_ms);
    CREATE INDEX IF NOT EXISTS idx_analytics_user    ON analytics (user_id);
";

const INSERT_SQL: &str = "
    INSERT INTO analytics (
        event_id, event_type, timestamp_ms, user_id, session_id, source,
        application, environment, schema_version, payload_json, attributes, received_at_ms
    ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12)
    ON CONFLICT(event_id) DO NOTHING
";

#[derive(Serialize, Deserialize)]
pub struct StoredEvent {
    pub event_id: String,
    pub event_type: String,
    pub timestamp_ms: i64,
    pub user_id: String,
    pub session_id: String,
    pub source: String,
    pub application: String,
    pub environment: String,
    pub schema_version: u32,
    pub payload_json: String,
    pub attributes: BTreeMap<String, String>,
    pub received_at_ms: i64,
}

impl StoredEvent {
    fn from_event(e: &AnalyticsEvent, received_at_ms: i64) -> Self {
        Self {
            event_id: e.event_id.clone(),
            event_type: e.event_type.clone(),
            timestamp_ms: e
                .timestamp
                .as_ref()
                .map(timestamp_to_millis)
                .unwrap_or(received_at_ms),
            user_id: e.user_id.clone(),
            session_id: e.session_id.clone(),
            source: e.source.clone(),
            application: e.application.clone(),
            environment: e.environment.clone(),
            schema_version: e.schema_version,
            payload_json: e.payload_json.clone(),
            attributes: e.attributes.iter().map(|(k, v)| (k.clone(), v.clone())).collect(),
            received_at_ms,
        }
    }
}

pub fn encode_chunk(events: &[AnalyticsEvent], received_at_ms: i64) -> anyhow::Result<Vec<u8>> {
    let stored: Vec<StoredEvent> = events
        .iter()
        .map(|e| StoredEvent::from_event(e, received_at_ms))
        .collect();
    let json = serde_json::to_vec(&stored)?;
    let compressed = zstd::encode_all(&json[..], ZSTD_LEVEL)?;

    let mut out = Vec::with_capacity(4 + compressed.len());
    out.extend_from_slice(&(events.len() as u32).to_le_bytes());
    out.extend_from_slice(&compressed);
    Ok(out)
}

fn chunk_event_count(element: &[u8]) -> u64 {
    element
        .get(..4)
        .map(|b| u64::from(u32::from_le_bytes([b[0], b[1], b[2], b[3]])))
        .unwrap_or(0)
}

fn decode_chunk(element: &[u8]) -> anyhow::Result<Vec<StoredEvent>> {
    let body = element.get(4..).context("queue element too short")?;
    let json = zstd::decode_all(body)?;
    Ok(serde_json::from_slice(&json)?)
}

pub fn setup_db_pool(db_path: &str) -> anyhow::Result<DbPool> {
    let manager = SqliteConnectionManager::file(db_path).with_init(|conn| {
        conn.execute_batch(
            "PRAGMA journal_mode = WAL;
             PRAGMA synchronous = NORMAL;
             PRAGMA cache_size = -64000;
             PRAGMA temp_store = MEMORY;
             PRAGMA busy_timeout = 5000;",
        )
    });

    let pool = r2d2::Pool::builder()
        .max_size(10)
        .build(manager)
        .context("Failed to create connection pool")?;

    pool.get()
        .context("Failed to get a connection from the pool")?
        .execute_batch(SCHEMA)
        .context("Error when creating tables")?;

    Ok(pool)
}

pub fn spawn_sqlite_writer(
    pool: DbPool,
    queue_path: PathBuf,
    rx: mpsc::Receiver<Vec<u8>>,
    metrics: Arc<Metrics>,
) -> anyhow::Result<std::thread::JoinHandle<()>> {
    let (ready_tx, ready_rx) = std::sync::mpsc::channel::<anyhow::Result<()>>();

    let handle = std::thread::Builder::new()
        .name("sqlite-writer".into())
        .spawn(move || {
            let queue = match QueueFile::open(&queue_path) {
                Ok(q) => {
                    let _ = ready_tx.send(Ok(()));
                    q
                }
                Err(e) => {
                    let _ = ready_tx.send(Err(anyhow::anyhow!(
                        "failed to open queue file {}: {}",
                        queue_path.display(),
                        e
                    )));
                    return;
                }
            };
            sqlite_writer_loop(pool, queue, rx, metrics);
        })?;

    ready_rx
        .recv()
        .context("writer thread exited during startup")??;
    Ok(handle)
}

pub fn sqlite_writer_loop(
    pool: DbPool,
    mut queue: QueueFile,
    mut rx: mpsc::Receiver<Vec<u8>>,
    metrics: Arc<Metrics>,
) {
    let recovered: u64 = queue.iter().map(|el| chunk_event_count(&el)).sum();
    if recovered > 0 {
        println!("Recovered {} unwritten events from the disk queue", recovered);
        metrics.total_received.fetch_add(recovered, Ordering::Relaxed);
        metrics.queue_depth.fetch_add(recovered, Ordering::Relaxed);
    }

    let mut closed = false;
    let mut shutdown_failures = 0;

    loop {
        let mut incoming: Vec<Vec<u8>> = Vec::new();
        if queue.is_empty() && !closed {
            match rx.blocking_recv() {
                Some(chunk) => incoming.push(chunk),
                None => closed = true,
            }
        }
        while !closed && incoming.len() < MAX_INTAKE_CHUNKS {
            match rx.try_recv() {
                Ok(chunk) => incoming.push(chunk),
                Err(TryRecvError::Empty) => break,
                Err(TryRecvError::Disconnected) => closed = true,
            }
        }

        if !incoming.is_empty() {
            append_to_queue(&mut queue, &incoming, &metrics);
        }

        while !queue.is_empty() {
            if let Err(e) = flush_batch(&mut queue, &pool, &metrics) {
                eprintln!("SQLite writer error (will retry): {:#}", e);
                if closed {
                    shutdown_failures += 1;
                    if shutdown_failures >= 3 {
                        eprintln!(
                            "Giving up on shutdown flush; {} events stay in the disk queue \
                             and will be replayed on the next start",
                            metrics.queue_depth.load(Ordering::Relaxed)
                        );
                        return;
                    }
                }
                std::thread::sleep(RETRY_DELAY);
            }
        }

        if closed {
            break;
        }
    }

    println!("SQLite writer stopped.");
}

fn append_to_queue(queue: &mut QueueFile, chunks: &[Vec<u8>], metrics: &Metrics) {
    for attempt in 1..=QUEUE_ADD_ATTEMPTS {
        match queue.add_n(chunks.iter()) {
            Ok(()) => return,
            Err(e) => {
                eprintln!(
                    "Queue append failed (attempt {}/{}): {}",
                    attempt, QUEUE_ADD_ATTEMPTS, e
                );
                std::thread::sleep(RETRY_DELAY);
            }
        }
    }

    let lost: u64 = chunks.iter().map(|c| chunk_event_count(c)).sum();
    eprintln!("Dropping {} events: disk queue unavailable", lost);
    metrics.total_failed.fetch_add(lost, Ordering::Relaxed);
    metrics.queue_depth.fetch_sub(lost, Ordering::Relaxed);
}

fn flush_batch(queue: &mut QueueFile, pool: &DbPool, metrics: &Metrics) -> anyhow::Result<()> {
    let elements: Vec<Box<[u8]>> = queue.iter().take(MAX_ELEMS_PER_TX).collect();
    if elements.is_empty() {
        return Ok(());
    }

    let mut conn = pool.get().context("connection pool busy")?;
    let tx = conn.transaction()?;

    let (mut handled, mut duplicates, mut unreadable, mut total) = (0u64, 0u64, 0u64, 0u64);
    {
        let mut stmt = tx.prepare_cached(INSERT_SQL)?;
        for element in &elements {
            let count = chunk_event_count(element);
            total += count;

            let events = match decode_chunk(element) {
                Ok(events) => events,
                Err(e) => {
                    eprintln!("Dropping unreadable queue element ({} events): {:#}", count, e);
                    unreadable += count;
                    continue;
                }
            };

            for ev in events {
                let attributes = serde_json::to_string(&ev.attributes)?;
                let changed = stmt.execute(params![
                    ev.event_id,
                    ev.event_type,
                    ev.timestamp_ms,
                    ev.user_id,
                    ev.session_id,
                    ev.source,
                    ev.application,
                    ev.environment,
                    ev.schema_version,
                    ev.payload_json,
                    attributes,
                    ev.received_at_ms,
                ])?;
                handled += 1;
                if changed == 0 {
                    duplicates += 1;
                }
            }
        }
    }
    tx.commit()?;
    queue.remove_n(elements.len())?;

    metrics.total_processed.fetch_add(handled, Ordering::Relaxed);
    metrics.total_duplicates.fetch_add(duplicates, Ordering::Relaxed);
    metrics.total_failed.fetch_add(unreadable, Ordering::Relaxed);
    metrics.queue_depth.fetch_sub(total, Ordering::Relaxed);
    if duplicates > 0 {
        println!("Skipped {} duplicate events", duplicates);
    }
    Ok(())
}
