//! keycard guest agent — runs as PID 1 (or PID 1's child) inside a keycard
//! Firecracker room. Listens on two ports, one session at a time (see
//! session.rs for why one-at-a-time is enough): a data port carrying raw
//! pty bytes, and a control port carrying resize requests in and the exit
//! status out. See FIRECRACKER.md, Phase 0.
//!
//! A connection is not a session. The shell outlives whichever connection is
//! attached to it, so that a host which dropped away — and, under `--keep`,
//! snapshotted the whole microVM in the meantime — can come back to the same
//! shell rather than a fresh one. `serve` below is where that decision is
//! made.
//!
//! Two transports share the same session logic: `vsock` is what a real
//! microVM guest uses; `tcp` is a loopback stand-in so this binary can be
//! built and exercised on a plain Linux host with no Firecracker involved.

mod session;

use std::process::ExitCode;

use anyhow::{Context, Result};
use tokio::io::{AsyncRead, AsyncWrite};
use tokio::net::TcpListener;
use tokio_vsock::{VsockAddr, VsockListener, VMADDR_CID_ANY};

use session::Session;

const DEFAULT_DATA_PORT: u32 = 10000;
const DEFAULT_CTRL_PORT: u32 = 10001;
const DEFAULT_SHELL: &str = "/bin/sh";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Transport {
    Vsock,
    Tcp,
}

struct Args {
    transport: Transport,
    data_port: u32,
    ctrl_port: u32,
    shell: String,
    tcp_bind: String,
}

fn parse_args() -> Result<Args> {
    let mut transport = Transport::Vsock;
    let mut data_port = DEFAULT_DATA_PORT;
    let mut ctrl_port = DEFAULT_CTRL_PORT;
    let mut shell = DEFAULT_SHELL.to_string();
    let mut tcp_bind = "127.0.0.1".to_string();

    let mut it = std::env::args().skip(1);
    while let Some(flag) = it.next() {
        let mut value = || {
            it.next()
                .with_context(|| format!("{flag} requires a value"))
        };
        match flag.as_str() {
            "--transport" => {
                transport = match value()?.as_str() {
                    "vsock" => Transport::Vsock,
                    "tcp" => Transport::Tcp,
                    other => anyhow::bail!("unknown --transport {other:?} (want vsock or tcp)"),
                };
            }
            "--data-port" => data_port = value()?.parse().context("--data-port")?,
            "--ctrl-port" => ctrl_port = value()?.parse().context("--ctrl-port")?,
            "--shell" => shell = value()?,
            "--bind" => tcp_bind = value()?,
            "-h" | "--help" => {
                print_usage();
                std::process::exit(0);
            }
            other => anyhow::bail!("unrecognized argument {other:?}"),
        }
    }

    Ok(Args {
        transport,
        data_port,
        ctrl_port,
        shell,
        tcp_bind,
    })
}

fn print_usage() {
    eprintln!(
        "keycard-guest-agent [--transport vsock|tcp] [--data-port N] [--ctrl-port N] \
         [--shell PATH] [--bind ADDR]\n\n\
         --transport tcp is a dev-only stand-in for vsock (see FIRECRACKER.md, Phase 0);\n\
         it is not what a real microVM guest uses."
    );
}

#[tokio::main]
async fn main() -> ExitCode {
    let args = match parse_args() {
        Ok(args) => args,
        Err(err) => {
            eprintln!("keycard-guest-agent: {err:#}");
            print_usage();
            return ExitCode::FAILURE;
        }
    };

    let result = match args.transport {
        Transport::Vsock => run_vsock(&args).await,
        Transport::Tcp => run_tcp(&args).await,
    };

    if let Err(err) = result {
        eprintln!("keycard-guest-agent: fatal: {err:#}");
        return ExitCode::FAILURE;
    }
    ExitCode::SUCCESS
}

async fn run_vsock(args: &Args) -> Result<()> {
    let data_listener = VsockListener::bind(VsockAddr::new(VMADDR_CID_ANY, args.data_port))
        .context("binding vsock data port")?;
    let ctrl_listener = VsockListener::bind(VsockAddr::new(VMADDR_CID_ANY, args.ctrl_port))
        .context("binding vsock control port")?;
    eprintln!(
        "keycard-guest-agent: listening on vsock data={} ctrl={}",
        args.data_port, args.ctrl_port
    );

    let mut live: Option<Session> = None;
    loop {
        let (data, _) = data_listener
            .accept()
            .await
            .context("accepting data connection")?;
        let (ctrl, _) = ctrl_listener
            .accept()
            .await
            .context("accepting control connection")?;
        live = Some(serve(live, data, ctrl, &args.shell).await?);
    }
}

async fn run_tcp(args: &Args) -> Result<()> {
    let data_listener = TcpListener::bind((args.tcp_bind.as_str(), args.data_port as u16))
        .await
        .context("binding tcp data port")?;
    let ctrl_listener = TcpListener::bind((args.tcp_bind.as_str(), args.ctrl_port as u16))
        .await
        .context("binding tcp control port")?;
    eprintln!(
        "keycard-guest-agent: [dev] listening on tcp {}:{} (data) and :{} (ctrl)",
        args.tcp_bind, args.data_port, args.ctrl_port
    );

    let mut live: Option<Session> = None;
    loop {
        let (data, _) = data_listener
            .accept()
            .await
            .context("accepting data connection")?;
        let (ctrl, _) = ctrl_listener
            .accept()
            .await
            .context("accepting control connection")?;
        live = Some(serve(live, data, ctrl, &args.shell).await?);
    }
}

/// Hand a new pair of connections to the session that is already running, or
/// start one if there is nothing to hand them to.
///
/// The distinction is what makes `--keep` work. A dropped connection is not
/// the end of a room: the host closes both channels, snapshots the microVM,
/// and reconnects inside the keep window expecting the same shell with the
/// same scrollback state (see FIRECRACKER.md, Phase 2). A shell that has
/// actually exited is a different matter — that room is over, and the next
/// connection is a new one.
async fn serve<D, C>(live: Option<Session>, data: D, ctrl: C, shell: &str) -> Result<Session>
where
    D: AsyncRead + AsyncWrite + Unpin + Send + 'static,
    C: AsyncRead + AsyncWrite + Unpin + Send + 'static,
{
    let mut session = match live {
        Some(session) if session.is_running() => {
            eprintln!("keycard-guest-agent: reattaching to the running session");
            session
        }
        _ => {
            eprintln!("keycard-guest-agent: session starting");
            Session::start(shell)?
        }
    };
    // Attaching only swaps writers and spawns reader tasks, so it returns
    // immediately — which matters, because the accept loop above is what
    // lets a reconnect displace a connection whose socket has not noticed it
    // is dead yet.
    session.attach(data, ctrl).await;
    Ok(session)
}
