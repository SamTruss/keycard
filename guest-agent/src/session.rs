//! One session: allocate a pty, exec the shell, bridge bytes, report how it ended.
//!
//! keycard only ever opens one channel per connection (see ARCHITECTURE.md,
//! "The mental model"), so a microVM only ever needs to run one session at a
//! time — there is no multiplexing to do here, unlike a real SSH server.
//!
//! What there *is* to do is survive the host going away. Under `--keep`, the
//! host closes both channels, snapshots the microVM, and reconnects later
//! expecting the same shell (see FIRECRACKER.md, Phase 2). So the shell is
//! not owned by a connection: it is owned by the `Session`, and a connection
//! is something that attaches to one. A dropped connection detaches; a new
//! one attaches to the shell that is already running.
//!
//! That is why the pty halves live in long-running tasks and the *host* side
//! is a slot those tasks write into, rather than the other way round.

use std::os::unix::io::{AsRawFd, RawFd};
use std::os::unix::process::ExitStatusExt;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

use anyhow::{Context, Result};
use pty_process::{Command, Size};
use tokio::io::{AsyncBufReadExt, AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt, BufReader};
use tokio::sync::{mpsc, Mutex};
use tokio::task::JoinHandle;

/// Fallback size used until the host's first "resize" line arrives on the
/// control channel — mirrors how DockerBackend.open() attaches before its
/// first real resize call.
const FALLBACK_ROWS: u16 = 24;
const FALLBACK_COLS: u16 = 80;

const BUF_SIZE: usize = 8192;

/// How much host input may be in flight towards the pty before the reader
/// task has to wait. Bounded so a host that floods the data channel cannot
/// grow the guest's memory without bound — a microVM's RAM is fixed at boot.
const INPUT_QUEUE: usize = 64;

/// How long the pty is given to flush after the shell exits, before the data
/// channel is closed. Without it the shell's last line — often the only
/// thing that says how it ended — races the teardown and usually loses.
const DRAIN_GRACE: Duration = Duration::from_millis(50);

/// The host end of a channel, whichever transport it arrived on.
type HostWriter = Box<dyn AsyncWrite + Unpin + Send>;

/// A writer that is only present while a host is attached. `None` means the
/// connection dropped and nobody is listening — output written then is
/// discarded, which is the right answer for a room whose client is gone: the
/// room survives, the scrollback does not.
type AttachedWriter = Arc<Mutex<Option<HostWriter>>>;

#[repr(C)]
struct Winsize {
    ws_row: u16,
    ws_col: u16,
    ws_xpixel: u16,
    ws_ypixel: u16,
}

fn set_winsize(fd: RawFd, rows: u16, cols: u16) -> std::io::Result<()> {
    let ws = Winsize {
        ws_row: rows,
        ws_col: cols,
        ws_xpixel: 0,
        ws_ypixel: 0,
    };
    // SAFETY: fd is a valid, open pty master for the lifetime of this call.
    // The task that owns the read half runs for as long as the session is
    // alive, and callers check `Session::is_running` before getting here.
    let ret = unsafe { libc::ioctl(fd, libc::TIOCSWINSZ, &ws as *const Winsize) };
    if ret != 0 {
        return Err(std::io::Error::last_os_error());
    }
    Ok(())
}

/// Parses a control-channel line. Wire format is deliberately tiny and
/// text-based (control traffic is rare, so readability beats density):
///
///   "resize <cols> <rows>"   host -> guest, applied via TIOCSWINSZ
///
/// The very first control line doubles as the initial size — there is no
/// separate "open" message, which keeps the protocol to one verb.
fn parse_resize(line: &str) -> Option<(u16, u16)> {
    let mut parts = line.split_whitespace();
    if parts.next()? != "resize" {
        return None;
    }
    let cols: u16 = parts.next()?.parse().ok()?;
    let rows: u16 = parts.next()?.parse().ok()?;
    Some((cols, rows))
}

fn exit_code(status: std::process::ExitStatus) -> i32 {
    // Killed-by-signal shells don't have a `code()`; fall back to the usual
    // shell convention (128 + signal number) rather than losing the signal.
    status
        .code()
        .unwrap_or_else(|| 128 + status.signal().unwrap_or(0))
}

/// A running shell on a pty, plus whichever host connection is attached to
/// it right now.
pub struct Session {
    /// Host input on its way to the pty. A channel rather than the pty write
    /// half directly, so that a new connection can start feeding the shell
    /// without taking ownership of anything the old one held.
    input: mpsc::Sender<Vec<u8>>,
    data_out: AttachedWriter,
    ctrl_out: AttachedWriter,
    raw_fd: RawFd,
    running: Arc<AtomicBool>,
    /// Tasks belonging to the current connection, aborted when another one
    /// takes over. The long-lived pty tasks are deliberately not in here.
    readers: Vec<JoinHandle<()>>,
}

impl Session {
    /// Spawn `shell` on a fresh pty. Nothing is attached yet — the shell
    /// starts running immediately, exactly as it would in a container that
    /// nobody has attached to.
    pub fn start(shell: &str) -> Result<Session> {
        let (pty, pts) = pty_process::open().context("allocating pty")?;
        pty.resize(Size::new(FALLBACK_ROWS, FALLBACK_COLS))
            .context("initial pty resize")?;
        // Captured before into_split(): Pty::resize() takes &self but
        // into_split() consumes self, and the raw fd is what TIOCSWINSZ
        // actually needs.
        let raw_fd = pty.as_raw_fd();

        // `-l`, not `--login`: dash rejects the long spelling outright and
        // exits, and dash is exactly what a room gets when its image has no
        // bash — rootfs/build.sh falls back to /bin/sh by design. bash takes
        // either, so the short form is the one that works everywhere.
        let mut child = Command::new(shell)
            .arg("-l")
            .spawn(pts)
            .with_context(|| format!("spawning shell {shell:?}"))?;

        let (mut pty_r, mut pty_w) = pty.into_split();
        let data_out: AttachedWriter = Arc::new(Mutex::new(None));
        let ctrl_out: AttachedWriter = Arc::new(Mutex::new(None));
        let running = Arc::new(AtomicBool::new(true));

        // pty -> whichever data connection is attached.
        let pump_out = {
            let data_out = data_out.clone();
            tokio::spawn(async move {
                let mut buf = vec![0u8; BUF_SIZE];
                loop {
                    let n = match pty_r.read(&mut buf).await {
                        Ok(0) | Err(_) => break,
                        Ok(n) => n,
                    };
                    let mut slot = data_out.lock().await;
                    if let Some(writer) = slot.as_mut() {
                        // A failed write means that connection is gone, not
                        // that the session is: clear the slot and keep
                        // reading, so the shell doesn't block on a full pty.
                        if writer.write_all(&buf[..n]).await.is_err() {
                            *slot = None;
                        }
                    }
                }
            })
        };

        // Queued host input -> pty. Ends when the Session is dropped and the
        // last sender with it.
        let (input, mut rx) = mpsc::channel::<Vec<u8>>(INPUT_QUEUE);
        tokio::spawn(async move {
            while let Some(chunk) = rx.recv().await {
                if pty_w.write_all(&chunk).await.is_err() {
                    break;
                }
            }
        });

        // The shell exiting is what ends a session — not the connection.
        {
            let data_out = data_out.clone();
            let ctrl_out = ctrl_out.clone();
            let running = running.clone();
            tokio::spawn(async move {
                let code = match child.wait().await {
                    Ok(status) => exit_code(status),
                    Err(_) => 1,
                };
                running.store(false, Ordering::SeqCst);

                // Let the last of the shell's output through before the
                // channel carrying it is closed.
                tokio::time::sleep(DRAIN_GRACE).await;
                pump_out.abort();

                if let Some(writer) = ctrl_out.lock().await.as_mut() {
                    let _ = writer.write_all(format!("exit {code}\n").as_bytes()).await;
                    let _ = writer.shutdown().await;
                }
                // Closing this is how the host learns the room ended: its
                // read returns empty, which is the contract Room.read has.
                if let Some(writer) = data_out.lock().await.as_mut() {
                    let _ = writer.shutdown().await;
                }
            });
        }

        Ok(Session {
            input,
            data_out,
            ctrl_out,
            raw_fd,
            running,
            readers: Vec::new(),
        })
    }

    /// Whether the shell is still alive. A session whose shell has exited is
    /// finished for good — the next connection gets a new one.
    pub fn is_running(&self) -> bool {
        self.running.load(Ordering::SeqCst)
    }

    /// Point the session at a new pair of host channels.
    ///
    /// Called for the first connection and for every reconnect after it. A
    /// second connection displaces the first rather than being refused:
    /// there is only ever one client for a room, so a new connection means
    /// the old one is gone, whether or not its socket has noticed yet.
    pub async fn attach<D, C>(&mut self, data: D, ctrl: C)
    where
        D: AsyncRead + AsyncWrite + Unpin + Send + 'static,
        C: AsyncRead + AsyncWrite + Unpin + Send + 'static,
    {
        for reader in self.readers.drain(..) {
            reader.abort();
        }

        let (mut data_r, data_w) = tokio::io::split(data);
        let (ctrl_r, ctrl_w) = tokio::io::split(ctrl);
        *self.data_out.lock().await = Some(Box::new(data_w));
        *self.ctrl_out.lock().await = Some(Box::new(ctrl_w));

        let input = self.input.clone();
        self.readers.push(tokio::spawn(async move {
            let mut buf = vec![0u8; BUF_SIZE];
            loop {
                let n = match data_r.read(&mut buf).await {
                    Ok(0) | Err(_) => break,
                    Ok(n) => n,
                };
                if input.send(buf[..n].to_vec()).await.is_err() {
                    break;
                }
            }
        }));

        let raw_fd = self.raw_fd;
        let running = self.running.clone();
        self.readers.push(tokio::spawn(async move {
            let mut lines = BufReader::new(ctrl_r).lines();
            while let Ok(Some(line)) = lines.next_line().await {
                if let Some((cols, rows)) = parse_resize(&line) {
                    // Checked because the fd belongs to a pty that only
                    // exists while the shell does.
                    if running.load(Ordering::SeqCst) {
                        let _ = set_winsize(raw_fd, rows, cols);
                    }
                }
            }
        }));
    }
}

impl Drop for Session {
    fn drop(&mut self) {
        for reader in self.readers.drain(..) {
            reader.abort();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_resize() {
        assert_eq!(parse_resize("resize 120 40"), Some((120, 40)));
    }

    #[test]
    fn rejects_other_verbs() {
        assert_eq!(parse_resize("exit 0"), None);
    }

    #[test]
    fn rejects_malformed_resize() {
        assert_eq!(parse_resize("resize 120"), None);
        assert_eq!(parse_resize("resize abc def"), None);
        assert_eq!(parse_resize(""), None);
    }

    #[test]
    fn exit_code_from_normal_exit() {
        use std::os::unix::process::ExitStatusExt;
        assert_eq!(exit_code(std::process::ExitStatus::from_raw(7 << 8)), 7);
    }

    #[test]
    fn exit_code_from_signal() {
        use std::os::unix::process::ExitStatusExt;
        // Killed by SIGKILL (9): shell convention is 128 + signal number.
        assert_eq!(exit_code(std::process::ExitStatus::from_raw(9)), 137);
    }

    /// The reattach contract, without a microVM: detach a session, attach a
    /// second connection, and check it is talking to the same shell.
    #[tokio::test]
    async fn a_reattached_session_is_the_same_shell() {
        use tokio::io::duplex;

        let mut session = Session::start("/bin/sh").expect("shell");
        let (host_data, guest_data) = duplex(BUF_SIZE);
        let (host_ctrl, guest_ctrl) = duplex(BUF_SIZE);
        session.attach(guest_data, guest_ctrl).await;

        let (mut host_data_r, mut host_data_w) = tokio::io::split(host_data);
        host_data_w
            .write_all(b"MARKER=still-here\n")
            .await
            .expect("write");
        read_until(&mut host_data_r, b"MARKER=still-here").await;

        // The host goes away: both channels close, the shell does not.
        drop(host_data_r);
        drop(host_data_w);
        drop(host_ctrl);

        let (host_data2, guest_data2) = duplex(BUF_SIZE);
        let (_host_ctrl2, guest_ctrl2) = duplex(BUF_SIZE);
        session.attach(guest_data2, guest_ctrl2).await;

        let (mut host_data_r2, mut host_data_w2) = tokio::io::split(host_data2);
        host_data_w2
            .write_all(b"echo $MARKER\n")
            .await
            .expect("write");
        let seen = read_until(&mut host_data_r2, b"still-here").await;

        assert!(session.is_running());
        assert!(String::from_utf8_lossy(&seen).contains("still-here"));
    }

    /// A shell that exits closes the data channel, which is how the host
    /// learns the room ended, and reports its status on the control channel.
    #[tokio::test]
    async fn an_exiting_shell_reports_and_closes() {
        use tokio::io::duplex;

        let mut session = Session::start("/bin/sh").expect("shell");
        let (host_data, guest_data) = duplex(BUF_SIZE);
        let (host_ctrl, guest_ctrl) = duplex(BUF_SIZE);
        session.attach(guest_data, guest_ctrl).await;

        let (mut host_data_r, mut host_data_w) = tokio::io::split(host_data);
        host_data_w.write_all(b"exit 7\n").await.expect("write");

        // Drains to EOF, which only happens because the agent shuts the
        // channel down on exit.
        let mut sink = Vec::new();
        let drained =
            tokio::time::timeout(Duration::from_secs(10), host_data_r.read_to_end(&mut sink)).await;
        assert!(drained.is_ok(), "data channel never closed");

        let mut ctrl = Vec::new();
        let (mut host_ctrl_r, _host_ctrl_w) = tokio::io::split(host_ctrl);
        let _ =
            tokio::time::timeout(Duration::from_secs(10), host_ctrl_r.read_to_end(&mut ctrl)).await;
        assert!(String::from_utf8_lossy(&ctrl).contains("exit 7"));
        assert!(!session.is_running());
    }

    async fn read_until<R: AsyncRead + Unpin>(reader: &mut R, needle: &[u8]) -> Vec<u8> {
        let mut seen = Vec::new();
        let mut buf = vec![0u8; BUF_SIZE];
        let deadline = tokio::time::Instant::now() + Duration::from_secs(10);
        while !seen.windows(needle.len()).any(|w| w == needle) {
            let read = tokio::time::timeout_at(deadline, reader.read(&mut buf)).await;
            match read {
                Ok(Ok(0)) | Err(_) => break,
                Ok(Ok(n)) => seen.extend_from_slice(&buf[..n]),
                Ok(Err(_)) => break,
            }
        }
        seen
    }
}
