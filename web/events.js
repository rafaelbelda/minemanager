/* Live node event sockets.
 *
 * One WebSocket per node (`/api/nodes/{id}/events`, push-only). The socket
 * carries every event for that node; consumers filter on `instance_id`.
 * Sockets reconnect with capped exponential backoff — a node whose agent is
 * offline simply keeps failing until it comes back.
 */

import { wsOrigin } from './api.js';

const BACKOFF_MIN = 1000;
const BACKOFF_MAX = 20000;

export class NodeEvents {
  /** @param {(ev: object, nodeId: string) => void} onEvent */
  constructor(onEvent) {
    this.onEvent = onEvent;
    this.conns = new Map(); // nodeId -> { ws, timer, backoff, open, closed }
  }

  /** Reconcile open sockets against the set of node ids that should have one. */
  sync(nodeIds) {
    const want = new Set(nodeIds);
    for (const id of [...this.conns.keys()]) {
      if (!want.has(id)) this.stop(id);
    }
    for (const id of want) {
      if (!this.conns.has(id)) this.start(id);
    }
  }

  start(nodeId) {
    if (this.conns.has(nodeId)) return;
    const conn = { ws: null, timer: null, backoff: BACKOFF_MIN, open: false, closed: false };
    this.conns.set(nodeId, conn);
    this.#connect(nodeId, conn);
  }

  stop(nodeId) {
    const conn = this.conns.get(nodeId);
    if (!conn) return;
    conn.closed = true;
    clearTimeout(conn.timer);
    // Drop handlers first so the close doesn't schedule a reconnect.
    if (conn.ws) {
      conn.ws.onclose = null;
      conn.ws.onerror = null;
      conn.ws.onmessage = null;
      conn.ws.close();
    }
    this.conns.delete(nodeId);
  }

  stopAll() {
    for (const id of [...this.conns.keys()]) this.stop(id);
  }

  #connect(nodeId, conn) {
    if (conn.closed) return;
  
    let ws;
    try {
      ws = new WebSocket(`${wsOrigin()}/api/nodes/${nodeId}/events`);
    } catch {
      this.#retry(nodeId, conn);
      return;
    }
  
    conn.ws = ws;
  
    ws.onopen = () => {
      conn.backoff = BACKOFF_MIN;
    };
  
    ws.onmessage = (msg) => {
      let frame;
      try { frame = JSON.parse(msg.data); } catch { return; }
      if (frame && frame.kind === 'event') this.onEvent(frame, nodeId);
    };
  
    ws.onerror = () => {
      /* onclose follows and drives the retry */
    };
  
    ws.onclose = () => {
      this.#retry(nodeId, conn);
    };
  }
  
  #retry(nodeId, conn) {
    if (conn.closed) return;
  
    clearTimeout(conn.timer);
  
    const wait = conn.backoff;
    conn.backoff = Math.min(conn.backoff * 2, BACKOFF_MAX);
    conn.timer = setTimeout(() => this.#connect(nodeId, conn), wait);
  }
}
