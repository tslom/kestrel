import { useEffect, useRef } from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import "@xterm/xterm/css/xterm.css";

// The live terminal of a computer-use job (backend console_terminal.py), in Claude's view: only a screen
// (keys down the socket, bytes back). The CLI's "allow this app?" prompt can only be answered here, by
// the user. The socket attaches to a running job; it can't start one.

const encoder = new TextEncoder();

// `path`: another terminal socket to attach to instead, e.g. a `!` command's
// (/ws/console/shell?id=…, ShellBlock.jsx). Same protocol.
export default function TerminalPane({ jobId, path, className = "" }) {
  const box = useRef(null);
  const src = path || `/ws/console/computer?id=${jobId}`;

  useEffect(() => {
    const css = getComputedStyle(box.current);
    const t = new Terminal({
      fontFamily: css.fontFamily, fontSize: 13, cursorBlink: true, scrollback: 5000,
      theme: { background: css.backgroundColor, foreground: css.color },
    });
    const fitter = new FitAddon();
    t.loadAddon(fitter);
    t.open(box.current);
    const sizeUp = () => { if (box.current?.offsetWidth) fitter.fit(); };
    sizeUp();

    const scheme = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${scheme}://${location.host}${src}`);
    ws.binaryType = "arraybuffer";
    const send = (data) => { if (ws.readyState === WebSocket.OPEN) ws.send(data); };
    const resize = () => send(JSON.stringify({ type: "resize", rows: t.rows, cols: t.cols }));
    ws.onopen = resize;
    ws.onmessage = ({ data }) => { if (typeof data !== "string") t.write(new Uint8Array(data)); };
    const typing = t.onData((data) => send(encoder.encode(data)));
    const sizing = t.onResize(resize);
    const seen = new ResizeObserver(sizeUp);
    seen.observe(box.current);
    t.focus();

    return () => {
      seen.disconnect();
      typing.dispose();
      sizing.dispose();
      ws.close();
      t.dispose();
    };
  }, [src]);

  return <div className={`terminal-pane ${className}`} ref={box} />;
}
