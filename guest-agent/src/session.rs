//! One session: allocate a pty, exec the shell, bridge bytes, report how it ended.
//!
//! keycard only ever opens one channel per connection (see ARCHITECTURE.md,
//! "The mental model"), so a microVM only ever needs to run one session at a
//! time — there is no multiplexing to do here, unlike a real SSH server.

use std::os::unix::io::{AsRawFd, RawFd};
use std::os::unix::process::ExitStatusExt;

use anyhow::{Context, Result};
use pty_process::{Command, Size};
use tokio::io::{AsyncBufReadExt, AsyncRead, AsyncWrite, AsyncWriteExt, BufReader};

/// Fallback size used until the host's first "resize" line arrives on the
/// control channel — mirrors how DockerBackend.open() attaches before its
/// first real resize call.
const FALLBACK_ROWS: u16 = 24;
const FALLBACK_COLS: u16 = 80;

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
    // SAFETY: fd is a valid, open pty master for the lifetime of this call —
    // it stays open because `session::run` keeps at least one split half of
    // the Pty alive for as long as this task can be scheduled.
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

/// Runs one session to completion: spawn `shell` on a fresh pty, bridge it
/// against `data`, service resize requests from `ctrl`, and write the exit
/// status to `ctrl` once the shell exits.
///
/// `data` and `ctrl` are generic over transport (vsock in the guest, TCP as
/// a dev-time stand-in — see main.rs) so this function doesn't know or care
/// which one it's holding.
pub async fn run<D, C>(data: D, ctrl: C, shell: &str) -> Result<()>
where
    D: AsyncRead + AsyncWrite + Unpin + Send + 'static,
    C: AsyncRead + AsyncWrite + Unpin + Send + 'static,
{
    let (pty, pts) = pty_process::open().context("allocating pty")?;
    pty.resize(Size::new(FALLBACK_ROWS, FALLBACK_COLS))
        .context("initial pty resize")?;
    // Captured before into_split() so resize keeps working once the pty is
    // split — Pty::resize() takes &self but into_split() consumes self, and
    // the raw fd is what TIOCSWINSZ actually needs.
    let raw_fd = pty.as_raw_fd();

    let mut child = Command::new(shell)
        .arg("--login")
        .spawn(pts)
        .with_context(|| format!("spawning shell {shell:?}"))?;

    let (mut pty_r, mut pty_w) = pty.into_split();
    let (mut data_r, mut data_w) = tokio::io::split(data);
    let (ctrl_r, mut ctrl_w) = tokio::io::split(ctrl);

    let copy_out = tokio::spawn(async move {
        let _ = tokio::io::copy(&mut pty_r, &mut data_w).await;
    });
    let copy_in = tokio::spawn(async move {
        let _ = tokio::io::copy(&mut data_r, &mut pty_w).await;
    });
    let resize = tokio::spawn(async move {
        let mut lines = BufReader::new(ctrl_r).lines();
        while let Ok(Some(line)) = lines.next_line().await {
            if let Some((cols, rows)) = parse_resize(&line) {
                let _ = set_winsize(raw_fd, rows, cols);
            }
        }
    });

    // Once the shell exits, the session is over — trailing pty bytes that
    // haven't been copied yet are a known Phase 0 gap (see FIRECRACKER.md);
    // Phase 2 tears down the whole microVM on destroy, which makes this
    // moot in production.
    let status = child.wait().await.context("waiting for shell")?;
    copy_out.abort();
    copy_in.abort();
    resize.abort();

    let code = exit_code(status);
    let _ = ctrl_w.write_all(format!("exit {code}\n").as_bytes()).await;
    let _ = ctrl_w.shutdown().await;

    Ok(())
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
}
