/**
 * MCS Stream D — API Client & React Hooks
 * =========================================
 * Connects to Stream B REST API (Task 5) and WebSocket feeds.
 * All dashboard components consume data through these hooks.
 */

// ── Configuration ───────────────────────────────────────────────────────

const API_BASE = import.meta?.env?.VITE_MCS_API_URL || "http://localhost:8000/api/v1";
const WS_BASE = import.meta?.env?.VITE_MCS_WS_URL || "ws://localhost:8000/api/v1";

const DEFAULT_HEADERS = {
  "Content-Type": "application/json",
};

// ── Base Fetch ──────────────────────────────────────────────────────────

class MCSApiError extends Error {
  constructor(status, detail) {
    super(detail || `API error ${status}`);
    this.status = status;
    this.detail = detail;
  }
}

async function apiFetch(path, options = {}) {
  const apiKey = localStorage.getItem("mcs_api_key") || "";
  const res = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers: {
      ...DEFAULT_HEADERS,
      ...(apiKey ? { "X-API-Key": apiKey } : {}),
      ...options.headers,
    },
  });

  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new MCSApiError(res.status, body.detail || res.statusText);
  }

  return res.json();
}

// ── REST API Methods ────────────────────────────────────────────────────

export const api = {
  // Sites
  listSites: (offset = 0, limit = 100) =>
    apiFetch(`/sites?offset=${offset}&limit=${limit}`),

  getSite: (slug) =>
    apiFetch(`/sites/${slug}`),

  // Blocks
  listBlocks: (siteSlug = null) =>
    apiFetch(`/blocks${siteSlug ? `?site_slug=${siteSlug}` : ""}`),

  getBlock: (slug) =>
    apiFetch(`/blocks/${slug}`),

  listEquipment: (blockSlug) =>
    apiFetch(`/equipment/${blockSlug}`),

  listSensors: (blockSlug, subsystem = null) =>
    apiFetch(`/sensors/${blockSlug}${subsystem ? `?subsystem=${subsystem}` : ""}`),

  // Telemetry
  queryTelemetry: (sensorId, start, end, agg = null) => {
    const params = new URLSearchParams({
      sensor_id: sensorId,
      start: start.toISOString(),
      end: end.toISOString(),
    });
    if (agg) params.set("agg", agg);
    return apiFetch(`/telemetry?${params}`);
  },

  getLatestValues: (blockSlug, subsystem = null) => {
    const params = new URLSearchParams({ block_slug: blockSlug });
    if (subsystem) params.set("subsystem", subsystem);
    return apiFetch(`/telemetry/latest?${params}`);
  },

  queryMultiSensor: (sensorIds, start, end, agg = null) => {
    const params = new URLSearchParams({
      sensor_ids: sensorIds.join(","),
      start: start.toISOString(),
      end: end.toISOString(),
    });
    if (agg) params.set("agg", agg);
    return apiFetch(`/telemetry/multi?${params}`);
  },

  // Alarms
  listAlarms: ({ state, priority, blockSlug, siteSlug, offset, limit } = {}) => {
    const params = new URLSearchParams();
    if (state) params.set("state", state);
    if (priority) params.set("priority", priority);
    if (blockSlug) params.set("block_slug", blockSlug);
    if (siteSlug) params.set("site_slug", siteSlug);
    if (offset) params.set("offset", offset);
    if (limit) params.set("limit", limit);
    return apiFetch(`/alarms?${params}`);
  },

  acknowledgeAlarm: (sensorId, operator) =>
    apiFetch(`/alarms/${sensorId}/acknowledge`, {
      method: "POST",
      body: JSON.stringify({ operator }),
    }),

  shelveAlarm: (sensorId, operator, reason, durationHours = 8) =>
    apiFetch(`/alarms/${sensorId}/shelve`, {
      method: "POST",
      body: JSON.stringify({ operator, reason, duration_hours: durationHours }),
    }),

  alarmStats: (blockSlug = null) =>
    apiFetch(`/alarms/stats${blockSlug ? `?block_slug=${blockSlug}` : ""}`),

  // Events
  listEvents: ({ blockSlug, eventType, start, end, offset, limit } = {}) => {
    const params = new URLSearchParams();
    if (blockSlug) params.set("block_slug", blockSlug);
    if (eventType) params.set("event_type", eventType);
    if (start) params.set("start", start.toISOString());
    if (end) params.set("end", end.toISOString());
    if (offset) params.set("offset", offset);
    if (limit) params.set("limit", limit);
    return apiFetch(`/events?${params}`);
  },

  // Billing
  billingKwh: (blockSlug, start, end) =>
    apiFetch(`/billing/kwh?block_slug=${blockSlug}&start=${start.toISOString()}&end=${end.toISOString()}`),

  billingKwht: (blockSlug, start, end) =>
    apiFetch(`/billing/kwht?block_slug=${blockSlug}&start=${start.toISOString()}&end=${end.toISOString()}`),

  energyDailySummary: (blockSlug, start, end) =>
    apiFetch(`/billing/energy-daily?block_slug=${blockSlug}&start=${start.toISOString()}&end=${end.toISOString()}`),

  // Health
  health: () => apiFetch("/health"),
  stats: () => apiFetch("/stats"),
};


// ── WebSocket Manager ───────────────────────────────────────────────────

export class WSConnection {
  constructor(path, { onMessage, onOpen, onClose, onError } = {}) {
    this.path = path;
    this.callbacks = { onMessage, onOpen, onClose, onError };
    this.ws = null;
    this.reconnectAttempts = 0;
    this.maxReconnectAttempts = 10;
    this.reconnectDelay = 1000;
    this.shouldReconnect = true;
  }

  connect() {
    const url = `${WS_BASE}${this.path}`;
    this.ws = new WebSocket(url);

    this.ws.onopen = () => {
      this.reconnectAttempts = 0;
      this.callbacks.onOpen?.();
    };

    this.ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        this.callbacks.onMessage?.(data);
      } catch {
        this.callbacks.onMessage?.(event.data);
      }
    };

    this.ws.onclose = () => {
      this.callbacks.onClose?.();
      if (this.shouldReconnect && this.reconnectAttempts < this.maxReconnectAttempts) {
        const delay = this.reconnectDelay * Math.pow(2, this.reconnectAttempts);
        this.reconnectAttempts++;
        setTimeout(() => this.connect(), Math.min(delay, 30000));
      }
    };

    this.ws.onerror = (err) => {
      this.callbacks.onError?.(err);
    };
  }

  send(data) {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(typeof data === "string" ? data : JSON.stringify(data));
    }
  }

  disconnect() {
    this.shouldReconnect = false;
    this.ws?.close();
  }
}


// ── React Hooks ─────────────────────────────────────────────────────────

import { useState, useEffect, useRef, useCallback } from "react";

/**
 * Fetch data from the API with loading/error states.
 * Auto-refetches when deps change.
 */
export function useApiQuery(fetchFn, deps = []) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const refetch = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await fetchFn();
      setData(result);
    } catch (err) {
      setError(err);
    } finally {
      setLoading(false);
    }
  }, deps);

  useEffect(() => { refetch(); }, [refetch]);

  return { data, loading, error, refetch };
}

/**
 * Poll an API endpoint at a fixed interval.
 */
export function usePolling(fetchFn, intervalMs = 5000, deps = []) {
  const result = useApiQuery(fetchFn, deps);

  useEffect(() => {
    const timer = setInterval(result.refetch, intervalMs);
    return () => clearInterval(timer);
  }, [intervalMs, result.refetch]);

  return result;
}

/**
 * Live telemetry feed via WebSocket.
 * Returns latest readings as a map: { [sensorId]: { value, quality, timestamp } }
 */
export function useLiveTelemetry(blockSlug, subsystem = null) {
  const [readings, setReadings] = useState({});
  const wsRef = useRef(null);

  useEffect(() => {
    if (!blockSlug) return;

    const path = `/ws/telemetry/${blockSlug}${subsystem ? `?subsystem=${subsystem}` : ""}`;
    const ws = new WSConnection(path, {
      onMessage: (data) => {
        setReadings((prev) => ({
          ...prev,
          [data.sensor_id]: {
            value: data.value,
            quality: data.quality,
            timestamp: data.timestamp,
            tag: data.tag,
            subsystem: data.subsystem,
          },
        }));
      },
    });
    ws.connect();
    wsRef.current = ws;

    return () => ws.disconnect();
  }, [blockSlug, subsystem]);

  return readings;
}

/**
 * Live alarm events via WebSocket.
 * Returns alarm events and a current alarm list.
 */
export function useLiveAlarms(blockSlug = null, minPriority = null) {
  const [events, setEvents] = useState([]);
  const [alarms, setAlarms] = useState([]);

  useEffect(() => {
    const params = new URLSearchParams();
    if (blockSlug) params.set("block_slug", blockSlug);
    if (minPriority) params.set("min_priority", minPriority);
    const qs = params.toString();

    const ws = new WSConnection(`/ws/alarms${qs ? `?${qs}` : ""}`, {
      onMessage: (data) => {
        setEvents((prev) => [data, ...prev.slice(0, 99)]);
        if (data.alarm) {
          setAlarms((prev) => {
            const idx = prev.findIndex((a) => a.sensor_id === data.alarm.sensor_id);
            if (data.event === "alarm_cleared") {
              return prev.filter((a) => a.sensor_id !== data.alarm.sensor_id);
            }
            if (idx >= 0) {
              const next = [...prev];
              next[idx] = data.alarm;
              return next;
            }
            return [data.alarm, ...prev];
          });
        }
      },
    });
    ws.connect();
    return () => ws.disconnect();
  }, [blockSlug, minPriority]);

  return { events, alarms };
}

/**
 * Telemetry history query with automatic tier selection.
 */
export function useTelemetryHistory(sensorId, start, end, agg = null) {
  return useApiQuery(
    () => sensorId && start && end ? api.queryTelemetry(sensorId, start, end, agg) : null,
    [sensorId, start?.getTime(), end?.getTime(), agg]
  );
}

/**
 * Block latest values — polls every 5 seconds.
 */
export function useBlockLatest(blockSlug) {
  return usePolling(
    () => blockSlug ? api.getLatestValues(blockSlug) : null,
    5000,
    [blockSlug]
  );
}

/**
 * Alarm stats (ISA-18.2 compliance) — polls every 30 seconds.
 */
export function useAlarmStats(blockSlug = null) {
  return usePolling(
    () => api.alarmStats(blockSlug),
    30000,
    [blockSlug]
  );
}

/**
 * Energy daily summary for billing dashboards.
 */
export function useEnergyDaily(blockSlug, start, end) {
  return useApiQuery(
    () => blockSlug && start && end ? api.energyDailySummary(blockSlug, start, end) : null,
    [blockSlug, start?.getTime(), end?.getTime()]
  );
}
