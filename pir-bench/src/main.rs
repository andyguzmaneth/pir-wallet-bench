//! Concurrent load test for a PIR endpoint.
//!
//! Each worker owns one `PirClient` (its own manifest and connection), so the
//! numbers describe the server under N simultaneous wallets, without the
//! single-client lock in local-pir-rpc. PIR requests have a fixed shape, so the
//! key does not change the cost; random keys are the default.

use std::io::{BufRead, Write};
use std::sync::{Arc, Mutex};
use std::time::{Instant, SystemTime, UNIX_EPOCH};

use clap::Parser;
use rand::RngCore;
use serde_json::json;

#[derive(Parser, Debug)]
struct Args {
    #[arg(long, env = "PIR_URL")]
    pir_url: String,
    /// Comma list of concurrency levels, run one after the other.
    #[arg(long, default_value = "1,4,16")]
    concurrency: String,
    /// Lookups per worker at each level.
    #[arg(long, default_value_t = 5)]
    lookups: usize,
    /// Optional file of 0x addresses (one per line) to query instead of random keys.
    #[arg(long)]
    keys: Option<std::path::PathBuf>,
    /// JSONL output (appended).
    #[arg(long)]
    out: std::path::PathBuf,
    /// Free-form tag stored on every line.
    #[arg(long, default_value = "")]
    tag: String,
    /// Before the load levels, run N timing probes that split a lookup into
    /// network round trip and upload. Each probe sends a /lookup body one byte
    /// short: the server reads it all, then rejects it without GPU work.
    #[arg(long, default_value_t = 0)]
    probes: usize,
    /// Size of a real lookup body in bytes (two packed queries).
    #[arg(long, default_value_t = 759_808)]
    lookup_bytes: usize,
}

/// One probe on a warm connection: the time to upload a full-size body and get
/// the rejection back (one round trip + upload, no server compute). Paired with
/// a TCP connect time (one round trip) and a real lookup taken right after, so
/// all three see the same network conditions.
fn probe(agent: &ureq::Agent, url: &str, lookup_bytes: usize) -> (Option<f64>, f64, Option<u16>) {
    let hostport = url.trim_start_matches("http://").trim_start_matches("https://")
        .split('/').next().unwrap_or("").to_string();
    let t = Instant::now();
    let connect = std::net::TcpStream::connect(&hostport).ok().map(|_| t.elapsed().as_secs_f64() * 1e3);
    let body = vec![0u8; lookup_bytes.saturating_sub(1)];
    let t = Instant::now();
    let r = agent.post(&format!("{}/lookup", url.trim_end_matches('/'))).send(&body[..]);
    let status = r.as_ref().ok().map(|x| x.status().as_u16());
    if let Ok(mut resp) = r {
        let _ = resp.body_mut().read_to_vec();
    }
    (connect, t.elapsed().as_secs_f64() * 1e3, status)
}

fn now_ms() -> f64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map_or(0.0, |d| d.as_secs_f64() * 1e3)
}

fn main() {
    let args = Args::parse();
    let keys: Arc<Vec<[u8; 20]>> = Arc::new(match &args.keys {
        Some(p) => std::io::BufReader::new(std::fs::File::open(p).expect("open keys"))
            .lines()
            .map_while(Result::ok)
            .filter_map(|l| {
                let h = l.trim().trim_start_matches("0x").to_string();
                hex::decode(h).ok()?.try_into().ok()
            })
            .collect(),
        None => Vec::new(),
    });
    let out = Arc::new(Mutex::new(
        std::fs::OpenOptions::new().create(true).append(true).open(&args.out).expect("open out"),
    ));

    if args.probes > 0 {
        let agent: ureq::Agent = ureq::Agent::config_builder().http_status_as_error(false).build().into();
        let mut client = pir_client::PirClient::connect(&args.pir_url).expect("connect");
        // Warm both connections so neither pays TCP slow start in the measured pairs.
        let _ = probe(&agent, &args.pir_url, args.lookup_bytes);
        let _ = client.lookup_address(&[0u8; 20]);
        let mut rng = rand::thread_rng();
        for i in 0..args.probes {
            let (connect, upload_rtt, status) = probe(&agent, &args.pir_url, args.lookup_bytes);
            let mut k = [0u8; 20];
            rng.fill_bytes(&mut k);
            let t = Instant::now();
            let r = client.lookup_address(&k);
            let total = t.elapsed().as_secs_f64() * 1e3;
            let m = &client.last_metrics;
            let ms = |us: u64| us as f64 / 1e3;
            writeln!(out.lock().unwrap(), "{}", json!({
                "kind": "probe", "tag": args.tag, "ts": now_ms(), "i": i,
                "connect_ms": connect, "upload_rtt_ms": upload_rtt, "probe_status": status,
                "upload_bytes": args.lookup_bytes - 1,
                "lookup_ok": r.is_ok(), "lookup_total_ms": total,
                "query_build_ms": ms(m.query_build_us), "http_ms": ms(m.http_us),
                "body_ms": ms(m.body_us), "decode_ms": ms(m.decode_us),
                "req_bytes": m.req_bytes, "resp_bytes": m.resp_bytes, "sidecar_bytes": m.sidecar_bytes,
            })).unwrap();
        }
        eprintln!("{} probe pairs done", args.probes);
    }

    for level in args.concurrency.split(',').filter_map(|s| s.trim().parse::<usize>().ok()) {
        let t_level = Instant::now();
        let handles: Vec<_> = (0..level)
            .map(|w| {
                let url = args.pir_url.clone();
                let keys = Arc::clone(&keys);
                let out = Arc::clone(&out);
                let tag = args.tag.clone();
                let n = args.lookups;
                std::thread::spawn(move || {
                    let emit = |v: serde_json::Value| {
                        let mut f = out.lock().unwrap();
                        writeln!(f, "{v}").unwrap();
                    };
                    let t_conn = Instant::now();
                    let mut client = match pir_client::PirClient::connect(&url) {
                        Ok(c) => c,
                        Err(e) => {
                            emit(json!({"kind":"error","tag":tag,"concurrency":level,"worker":w,"stage":"connect","error":e}));
                            return;
                        }
                    };
                    emit(json!({"kind":"connect","tag":tag,"concurrency":level,"worker":w,
                        "connect_ms": t_conn.elapsed().as_secs_f64()*1e3}));
                    let mut rng = rand::thread_rng();
                    for i in 0..n {
                        let key: [u8; 20] = if keys.is_empty() {
                            let mut k = [0u8; 20];
                            rng.fill_bytes(&mut k);
                            k
                        } else {
                            keys[(w * n + i) % keys.len()]
                        };
                        let ts = now_ms();
                        let t = Instant::now();
                        let r = client.lookup_address(&key);
                        let total = t.elapsed().as_secs_f64() * 1e3;
                        let m = &client.last_metrics;
                        let ms = |us: u64| us as f64 / 1e3;
                        emit(json!({
                            "kind": "lookup", "tag": tag, "ts": ts, "concurrency": level,
                            "worker": w, "i": i, "ok": r.is_ok(),
                            "error": r.as_ref().err(),
                            "total_ms": total,
                            "query_build_ms": ms(m.query_build_us), "http_ms": ms(m.http_us),
                            "body_ms": ms(m.body_us), "decode_ms": ms(m.decode_us),
                            "req_bytes": m.req_bytes, "resp_bytes": m.resp_bytes,
                            "sidecar_bytes": m.sidecar_bytes, "sidecar_entries": m.sidecar_entries,
                            "snapshot_block": m.snapshot_block, "source": m.source,
                        }));
                    }
                })
            })
            .collect();
        for h in handles {
            let _ = h.join();
        }
        let wall = t_level.elapsed().as_secs_f64();
        let total = level * args.lookups;
        writeln!(out.lock().unwrap(), "{}", json!({"kind":"level","tag":args.tag,"concurrency":level,"lookups":total,
            "wall_s":wall,"throughput_per_s": total as f64 / wall})).unwrap();
        eprintln!("c={level:<3} {total:>4} lookups in {wall:6.1}s  ({:.2}/s)", total as f64 / wall);
    }
}
