"use client";

import { useEffect, useRef, useState } from "react";
import { MarketSnapshot, WS_URL } from "./api";

type Status = "connecting" | "open" | "closed";

/**
 * Subscribes to the backend WebSocket and returns the latest MarketSnapshot.
 * Auto-reconnects with a short backoff.
 */
export function useLiveData() {
  const [snapshot, setSnapshot] = useState<MarketSnapshot | null>(null);
  const [status, setStatus] = useState<Status>("connecting");
  const wsRef = useRef<WebSocket | null>(null);
  const retryRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    let closedByUs = false;

    const connect = () => {
      setStatus("connecting");
      const ws = new WebSocket(WS_URL);
      wsRef.current = ws;

      ws.onopen = () => setStatus("open");
      ws.onmessage = (ev) => {
        try {
          setSnapshot(JSON.parse(ev.data) as MarketSnapshot);
        } catch {
          /* ignore malformed frame */
        }
      };
      ws.onclose = () => {
        setStatus("closed");
        if (!closedByUs) {
          retryRef.current = setTimeout(connect, 2000);
        }
      };
      ws.onerror = () => ws.close();
    };

    connect();

    return () => {
      closedByUs = true;
      if (retryRef.current) clearTimeout(retryRef.current);
      wsRef.current?.close();
    };
  }, []);

  return { snapshot, status };
}
