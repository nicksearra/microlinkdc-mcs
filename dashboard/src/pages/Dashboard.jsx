import { useState, useEffect, useRef, useCallback, useMemo } from "react";
import { AreaChart, Area, XAxis, YAxis, Tooltip, ResponsiveContainer, ReferenceLine } from "recharts";

// ═══════════════════════════════════════════════════════════════════════════
// MCS NOC DASHBOARD v2 — CINEMATIC INDUSTRIAL
// ═══════════════════════════════════════════════════════════════════════════
// Inspired by: CHI SCADA, Chinese DC command centers, Moscow energy flows
// Glowing edges, radial gauges, animated thermal loops, rack heatmaps

const generateTrend = (hrs, base, noise, drift = 0) => {
  const now = Date.now();
  return Array.from({ length: hrs * 12 }, (_, i) => {
    const t = now - (hrs * 12 - i) * 300000;
    const d = Math.sin((i / (hrs * 12)) * Math.PI * 2) * drift;
    return {
      time: t,
      label: new Date(t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }),
      value: +(base + d + (Math.random() - 0.5) * noise).toFixed(2),
    };
  });
};

// ── Radial Gauge ────────────────────────────────────────────────────────
const RadialGauge = ({ value, min, max, label, unit, size = 120, color = "#06b6d4", thresholds }) => {
  const pct = Math.min(1, Math.max(0, (value - min) / (max - min)));
  const angle = pct * 270 - 135;
  const r = size / 2 - 12;
  const cx = size / 2, cy = size / 2;

  const arcPath = (startAngle, endAngle) => {
    const s = ((startAngle - 90) * Math.PI) / 180;
    const e = ((endAngle - 90) * Math.PI) / 180;
    const x1 = cx + r * Math.cos(s), y1 = cy + r * Math.sin(s);
    const x2 = cx + r * Math.cos(e), y2 = cy + r * Math.sin(e);
    const large = endAngle - startAngle > 180 ? 1 : 0;
    return `M ${x1} ${y1} A ${r} ${r} 0 ${large} 1 ${x2} ${y2}`;
  };

  const needleEnd = {
    x: cx + (r - 8) * Math.cos(((angle - 90) * Math.PI) / 180),
    y: cy + (r - 8) * Math.sin(((angle - 90) * Math.PI) / 180),
  };

  const activeColor = thresholds
    ? value >= thresholds.red ? "#ef4444" : value >= thresholds.yellow ? "#f59e0b" : "#10b981"
    : color;

  return (
    <div style={{ textAlign: "center" }}>
      <svg width={size} height={size * 0.85} viewBox={`0 0 ${size} ${size * 0.85}`}>
        {/* Background arc */}
        <path d={arcPath(-135, 135)} fill="none" stroke="rgba(255,255,255,0.06)" strokeWidth={6} strokeLinecap="round" />
        {/* Active arc */}
        <path d={arcPath(-135, angle)} fill="none" stroke={activeColor} strokeWidth={6} strokeLinecap="round"
          style={{ filter: `drop-shadow(0 0 6px ${activeColor})`, transition: "all 0.8s ease" }} />
        {/* Tick marks */}
        {[0, 0.25, 0.5, 0.75, 1].map((p, i) => {
          const a = ((p * 270 - 135 - 90) * Math.PI) / 180;
          const x1t = cx + (r + 4) * Math.cos(a), y1t = cy + (r + 4) * Math.sin(a);
          const x2t = cx + (r + 9) * Math.cos(a), y2t = cy + (r + 9) * Math.sin(a);
          return <line key={i} x1={x1t} y1={y1t} x2={x2t} y2={y2t} stroke="rgba(255,255,255,0.2)" strokeWidth={1} />;
        })}
        {/* Needle */}
        <line x1={cx} y1={cy} x2={needleEnd.x} y2={needleEnd.y} stroke={activeColor} strokeWidth={2}
          style={{ filter: `drop-shadow(0 0 4px ${activeColor})`, transition: "all 0.8s ease" }} />
        <circle cx={cx} cy={cy} r={3} fill={activeColor} />
        {/* Value */}
        <text x={cx} y={cy + 18} textAnchor="middle" fill="#e2e8f0" fontSize={size * 0.22} fontWeight="800"
          fontFamily="'JetBrains Mono', monospace">{typeof value === "number" ? value.toFixed(2) : value}</text>
        <text x={cx} y={cy + 30} textAnchor="middle" fill="#64748b" fontSize={9}>{unit}</text>
      </svg>
      <div style={{ fontSize: 10, color: "#64748b", marginTop: -4, letterSpacing: "0.5px", textTransform: "uppercase" }}>{label}</div>
    </div>
  );
};

// ── Animated Thermal Flow SVG ───────────────────────────────────────────
const ThermalFlowDiagram = ({ mode, itLoad, supplyTemp, returnTemp, hostTemp, recoveryPct }) => {
  const w = 580, h = 220;
  return (
    <svg width="100%" viewBox={`0 0 ${w} ${h}`} style={{ overflow: "visible" }}>
      <defs>
        <linearGradient id="coldFlow" x1="0" y1="0" x2="1" y2="0"><stop offset="0%" stopColor="#3b82f6" /><stop offset="100%" stopColor="#06b6d4" /></linearGradient>
        <linearGradient id="hotFlow" x1="0" y1="0" x2="1" y2="0"><stop offset="0%" stopColor="#f97316" /><stop offset="100%" stopColor="#ef4444" /></linearGradient>
        <linearGradient id="hostFlow" x1="0" y1="0" x2="1" y2="0"><stop offset="0%" stopColor="#10b981" /><stop offset="100%" stopColor="#06b6d4" /></linearGradient>
        <filter id="glow"><feGaussianBlur stdDeviation="3" result="blur" /><feMerge><feMergeNode in="blur" /><feMergeNode in="SourceGraphic" /></feMerge></filter>
        {/* Animated dash pattern */}
        <style>{`
          .flow-cold { stroke-dasharray: 8 6; animation: flowRight 1.5s linear infinite; }
          .flow-hot { stroke-dasharray: 8 6; animation: flowRight 1.2s linear infinite; }
          .flow-host { stroke-dasharray: 8 6; animation: flowRight 2s linear infinite; }
          .flow-reject { stroke-dasharray: 8 6; animation: flowRight 1.8s linear infinite; }
          @keyframes flowRight { to { stroke-dashoffset: -28; } }
          .node-pulse { animation: npulse 3s ease-in-out infinite; }
          @keyframes npulse { 0%,100% { opacity:0.7 } 50% { opacity:1 } }
        `}</style>
      </defs>

      {/* Background grid */}
      {Array.from({ length: 30 }, (_, i) => (
        <line key={`g${i}`} x1={i * 20} y1={0} x2={i * 20} y2={h} stroke="rgba(255,255,255,0.02)" />
      ))}

      {/* LOOP 1: IT Cooling (CDU → Rack → CDU) */}
      <g>
        <text x={90} y={22} fill="#64748b" fontSize={9} fontWeight="700">LOOP 1 · IT COOLING</text>
        {/* Cold supply to racks */}
        <path d="M 60,55 L 180,55" fill="none" stroke="url(#coldFlow)" strokeWidth={3} className="flow-cold" filter="url(#glow)" />
        <polygon points="175,51 185,55 175,59" fill="#06b6d4" />
        {/* Hot return from racks */}
        <path d="M 180,75 L 60,75" fill="none" stroke="url(#hotFlow)" strokeWidth={3} className="flow-hot" filter="url(#glow)" />
        <polygon points="65,71 55,75 65,79" fill="#ef4444" />
        {/* CDU box */}
        <rect x={15} y={40} width={45} height={50} rx={6} fill="rgba(6,182,212,0.08)" stroke="#06b6d4" strokeWidth={1} />
        <text x={37} y={60} textAnchor="middle" fill="#06b6d4" fontSize={8} fontWeight="700">CDU</text>
        <text x={37} y={73} textAnchor="middle" fill="#e2e8f0" fontSize={10} fontWeight="800" fontFamily="monospace">{supplyTemp}°</text>
        {/* Rack box */}
        <rect x={180} y={35} width={60} height={60} rx={6} fill="rgba(59,130,246,0.08)" stroke="#3b82f6" strokeWidth={1} />
        <text x={210} y={55} textAnchor="middle" fill="#3b82f6" fontSize={8} fontWeight="700">IT RACKS</text>
        <text x={210} y={70} textAnchor="middle" fill="#e2e8f0" fontSize={12} fontWeight="800" fontFamily="monospace">{itLoad} kW</text>
        <text x={210} y={82} textAnchor="middle" fill="#ef4444" fontSize={9} fontFamily="monospace">{returnTemp}°C ret</text>
      </g>

      {/* LOOP 2: Primary Glycol (CDU → PHX) */}
      <g>
        <text x={90} y={118} fill="#64748b" fontSize={9} fontWeight="700">LOOP 2 · PRIMARY GLYCOL</text>
        <path d="M 60,90 L 60,135 L 280,135" fill="none" stroke="url(#hotFlow)" strokeWidth={3} className="flow-hot" filter="url(#glow)" />
        <polygon points="275,131 285,135 275,139" fill="#ef4444" />
        <path d="M 280,155 L 60,155 L 60,90" fill="none" stroke="url(#coldFlow)" strokeWidth={3} className="flow-cold" filter="url(#glow)" opacity={0.7} />
        {/* PHX box */}
        <rect x={280} y={120} width={55} height={50} rx={6} fill="rgba(16,185,129,0.08)" stroke="#10b981" strokeWidth={1} />
        <text x={307} y={140} textAnchor="middle" fill="#10b981" fontSize={8} fontWeight="700">PHX</text>
        <text x={307} y={155} textAnchor="middle" fill="#e2e8f0" fontSize={10} fontWeight="800" fontFamily="monospace">{hostTemp}°C</text>
      </g>

      {/* LOOP 3: Host Delivery */}
      <g>
        <text x={380} y={118} fill="#64748b" fontSize={9} fontWeight="700">LOOP 3 · HOST</text>
        <path d="M 335,135 L 440,135" fill="none" stroke="url(#hostFlow)" strokeWidth={3} className="flow-host" filter="url(#glow)" />
        <polygon points="435,131 445,135 435,139" fill="#10b981" />
        <path d="M 440,155 L 335,155" fill="none" stroke="rgba(16,185,129,0.4)" strokeWidth={2} className="flow-host" />
        {/* Host box */}
        <rect x={440} y={115} width={70} height={55} rx={8} fill="rgba(16,185,129,0.06)" stroke="#10b981" strokeWidth={1.5} strokeDasharray="4 2" />
        <text x={475} y={135} textAnchor="middle" fill="#10b981" fontSize={8} fontWeight="700">HOST PROCESS</text>
        <text x={475} y={152} textAnchor="middle" fill="#e2e8f0" fontSize={11} fontWeight="800" fontFamily="monospace">{recoveryPct}%</text>
        <text x={475} y={163} textAnchor="middle" fill="#64748b" fontSize={8}>recovered</text>
      </g>

      {/* Reject path (if partial/full rejection) */}
      {mode !== "FULL_RECOVERY" && (
        <g opacity={0.5}>
          <path d="M 307,170 L 307,195 L 520,195" fill="none" stroke="#f59e0b" strokeWidth={2} className="flow-reject" strokeDasharray="4 4" />
          <rect x={520} y={185} width={50} height={20} rx={4} fill="rgba(245,158,11,0.08)" stroke="#f59e0b" strokeWidth={1} />
          <text x={545} y={199} textAnchor="middle" fill="#f59e0b" fontSize={8} fontWeight="700">REJECT</text>
        </g>
      )}

      {/* Mode badge */}
      <rect x={380} y={40} width={110} height={28} rx={14} fill="rgba(6,182,212,0.12)" stroke="#06b6d4" strokeWidth={1} />
      <text x={435} y={58} textAnchor="middle" fill="#06b6d4" fontSize={10} fontWeight="800">{mode}</text>
    </svg>
  );
};

// ── Rack Heatmap Grid ───────────────────────────────────────────────────
const RackHeatmap = ({ racks }) => {
  const tempToColor = (t) => {
    if (t < 30) return "#10b981";
    if (t < 35) return "#22d3ee";
    if (t < 40) return "#3b82f6";
    if (t < 50) return "#f59e0b";
    if (t < 60) return "#f97316";
    return "#ef4444";
  };

  return (
    <div>
      <div style={{ fontSize: 10, color: "#64748b", marginBottom: 6, letterSpacing: "0.5px", textTransform: "uppercase" }}>
        Rack Inlet Temperatures
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(7, 1fr)", gap: 3 }}>
        {racks.map((rack, i) => (
          <div key={i} style={{
            background: tempToColor(rack.temp),
            borderRadius: 4, padding: "4px 2px", textAlign: "center",
            fontSize: 10, fontWeight: 700, fontFamily: "monospace",
            color: rack.temp > 45 ? "#fff" : "#0a0e17",
            boxShadow: rack.temp > 50 ? `0 0 8px ${tempToColor(rack.temp)}` : "none",
            transition: "all 0.5s",
          }}>
            <div style={{ fontSize: 8, opacity: 0.7 }}>R{(i + 1).toString().padStart(2, "0")}</div>
            {rack.temp.toFixed(0)}°
          </div>
        ))}
      </div>
      <div style={{ display: "flex", gap: 8, marginTop: 6, justifyContent: "center" }}>
        {[{ t: "<30°", c: "#10b981" }, { t: "30-40°", c: "#3b82f6" }, { t: "40-50°", c: "#f59e0b" }, { t: ">50°", c: "#ef4444" }].map(l => (
          <span key={l.t} style={{ fontSize: 9, color: "#64748b", display: "flex", alignItems: "center", gap: 3 }}>
            <span style={{ width: 8, height: 8, borderRadius: 2, background: l.c, display: "inline-block" }} />{l.t}
          </span>
        ))}
      </div>
    </div>
  );
};

// ── Alarm Ticker ────────────────────────────────────────────────────────
const AlarmTicker = ({ alarms }) => {
  const [offset, setOffset] = useState(0);
  useEffect(() => {
    const t = setInterval(() => setOffset(p => p + 1), 50);
    return () => clearInterval(t);
  }, []);

  const standing = alarms.filter(a => a.state === "ACTIVE");
  if (standing.length === 0) return null;

  const content = standing.map(a => `⚠ ${a.priority} ${a.tag} ${a.value.toFixed(1)} ${a.dir}${a.threshold}`).join("     ·     ");
  const doubled = content + "     ·     " + content;

  return (
    <div style={{
      background: "rgba(239,68,68,0.08)", borderTop: "1px solid rgba(239,68,68,0.3)",
      borderBottom: "1px solid rgba(239,68,68,0.3)", overflow: "hidden", height: 24,
      display: "flex", alignItems: "center", marginBottom: 0,
    }}>
      <div style={{
        whiteSpace: "nowrap", fontSize: 11, fontFamily: "monospace", fontWeight: 600,
        color: "#ef4444", transform: `translateX(-${offset % (content.length * 6.5)}px)`,
        textShadow: "0 0 8px rgba(239,68,68,0.4)",
      }}>{doubled}</div>
    </div>
  );
};

// ── Hero Metric ─────────────────────────────────────────────────────────
const HeroMetric = ({ label, value, unit, range, color = "#e2e8f0", warn }) => (
  <div style={{ flex: 1, textAlign: "center", padding: "8px 0", position: "relative" }}>
    <div style={{ fontSize: 9, color: "#475569", textTransform: "uppercase", letterSpacing: "1px", marginBottom: 2 }}>{label}</div>
    <div style={{
      fontSize: 32, fontWeight: 900, fontFamily: "'JetBrains Mono', monospace",
      color: warn ? "#ef4444" : color, lineHeight: 1,
      textShadow: warn ? "0 0 20px rgba(239,68,68,0.5)" : `0 0 12px ${color}33`,
      transition: "all 0.5s",
    }}>{typeof value === "number" ? value.toLocaleString(undefined, { minimumFractionDigits: 1, maximumFractionDigits: 1 }) : value}</div>
    <div style={{ display: "flex", justifyContent: "center", gap: 8, marginTop: 2 }}>
      <span style={{ fontSize: 9, color: "#475569" }}>{unit}</span>
      {range && <span style={{ fontSize: 9, color: "#334155" }}>0 — {range}</span>}
    </div>
  </div>
);

// ── Sensor Sparkline ────────────────────────────────────────────────────
const Sparkline = ({ data, width = 80, height = 24, color = "#3b82f6" }) => {
  if (!data || data.length < 2) return null;
  const vals = data.slice(-20).map(d => d.value);
  const mn = Math.min(...vals), mx = Math.max(...vals);
  const range = mx - mn || 1;
  const points = vals.map((v, i) => `${(i / (vals.length - 1)) * width},${height - ((v - mn) / range) * (height - 4) - 2}`).join(" ");
  return (
    <svg width={width} height={height} style={{ display: "block" }}>
      <polyline points={points} fill="none" stroke={color} strokeWidth={1.5} opacity={0.8} />
    </svg>
  );
};

// ── Main Dashboard ──────────────────────────────────────────────────────
export default function MCSDashboardV2() {
  const [tick, setTick] = useState(0);
  const [selectedChart, setSelectedChart] = useState("CDU-01-T-RET");

  useEffect(() => {
    const t = setInterval(() => setTick(p => p + 1), 3000);
    return () => clearInterval(t);
  }, []);

  // Simulated live jitter
  const jit = useCallback((base, range) => +(base + (Math.random() - 0.5) * range).toFixed(1), [tick]);

  const itLoad = jit(847, 10);
  const pue = jit(1.09, 0.02);
  const recovery = jit(87, 2);
  const supplyT = jit(31.2, 0.5);
  const returnT = jit(42.8, 1);
  const hostOutT = jit(41.2, 0.8);

  const racks = useMemo(() =>
    Array.from({ length: 14 }, (_, i) => ({
      id: `R${(i + 1).toString().padStart(2, "0")}`,
      temp: i < 4 ? jit(28, 3) : i < 11 ? jit(38, 6) : jit(52, 8),
      type: i < 4 ? "cloud" : i < 11 ? "ai" : "gpu",
    })), [tick]);

  const alarms = [
    { id: 1, priority: "P1", state: "ACTIVE", tag: "CDU-01-T-RET", value: 55.8, dir: "▲", threshold: 55.0, subsystem: "thermal-l1" },
    { id: 2, priority: "P2", state: "ACKED", tag: "ML-GLYCOL-CONC", value: 31.5, dir: "▼", threshold: 32.0, subsystem: "thermal-l2" },
  ];

  const sensors = [
    { tag: "CDU-01-T-SUP", desc: "CDU 01 Supply", val: supplyT, unit: "°C", sub: "thermal-l1", trend: generateTrend(4, 31, 1.5, 2), color: "#06b6d4" },
    { tag: "CDU-01-T-RET", desc: "CDU 01 Return", val: returnT, unit: "°C", sub: "thermal-l1", trend: generateTrend(4, 42, 2, 3), color: "#ef4444", warn: returnT > 50 },
    { tag: "CDU-01-FLOW", desc: "CDU 01 Flow", val: jit(85, 3), unit: "L/min", sub: "thermal-l1", trend: generateTrend(4, 85, 5, 3), color: "#3b82f6" },
    { tag: "ML-T-SUP", desc: "Glycol Supply", val: jit(28.3, 0.8), unit: "°C", sub: "thermal-l2", trend: generateTrend(4, 28, 1.5, 2), color: "#06b6d4" },
    { tag: "ML-T-RET", desc: "Glycol Return", val: jit(45.6, 1.2), unit: "°C", sub: "thermal-l2", trend: generateTrend(4, 45, 2, 3), color: "#f97316" },
    { tag: "ML-FLOW", desc: "Primary Flow", val: jit(340, 8), unit: "L/min", sub: "thermal-l2", trend: generateTrend(4, 340, 15, 10), color: "#8b5cf6" },
    { tag: "PHX-T-OUT", desc: "Host Water Out", val: hostOutT, unit: "°C", sub: "thermal-l3", trend: generateTrend(4, 41, 2, 2), color: "#10b981" },
    { tag: "P-TOTAL", desc: "Total IT Power", val: itLoad, unit: "kW", sub: "electrical", trend: generateTrend(4, 845, 20, 15), color: "#f59e0b" },
    { tag: "UPS-LOAD", desc: "UPS Load", val: jit(72, 2), unit: "%", sub: "electrical", trend: generateTrend(4, 72, 3, 2), color: "#3b82f6" },
    { tag: "ENV-T-AMB", desc: "Ambient", val: jit(22, 2), unit: "°C", sub: "environmental", trend: generateTrend(4, 22, 4, 3), color: "#64748b" },
  ];

  const chartSensor = sensors.find(s => s.tag === selectedChart) || sensors[0];

  return (
    <div style={{ minHeight: "100vh", background: "#060a12", color: "#e2e8f0", fontFamily: "'DM Sans', system-ui, sans-serif" }}>
      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=DM+Sans:ital,opsz,wght@0,9..40,400;0,9..40,700;0,9..40,800;0,9..40,900&family=JetBrains+Mono:wght@400;700;800&display=swap');
        * { box-sizing: border-box; margin: 0; padding: 0; }
        @keyframes scanline { 0% { top: -2px; } 100% { top: 100%; } }
        @keyframes borderGlow { 0%,100% { border-color: rgba(6,182,212,0.3); } 50% { border-color: rgba(6,182,212,0.6); } }
        .card { background: rgba(17,24,39,0.6); border: 1px solid rgba(30,41,59,0.8); border-radius: 10px; position: relative; overflow: hidden; backdrop-filter: blur(8px); }
        .card::before { content: ''; position: absolute; top: 0; left: 0; right: 0; height: 1px; background: linear-gradient(90deg, transparent, rgba(6,182,212,0.3), transparent); }
        .card-glow { animation: borderGlow 4s ease-in-out infinite; }
        .scanline { position: absolute; left: 0; right: 0; height: 2px; background: linear-gradient(90deg, transparent, rgba(6,182,212,0.08), transparent); animation: scanline 8s linear infinite; pointer-events: none; z-index: 1; }
        ::-webkit-scrollbar { width: 4px; } ::-webkit-scrollbar-track { background: transparent; } ::-webkit-scrollbar-thumb { background: #1e293b; border-radius: 2px; }
      `}</style>

      {/* ── TOP BAR ── */}
      <div style={{
        display: "flex", justifyContent: "space-between", alignItems: "center",
        padding: "8px 16px", borderBottom: "1px solid rgba(30,41,59,0.5)",
        background: "rgba(10,14,23,0.9)",
      }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <div style={{
            width: 28, height: 28, borderRadius: 6,
            background: "linear-gradient(135deg, #06b6d4, #3b82f6)",
            display: "flex", alignItems: "center", justifyContent: "center",
            fontSize: 14, fontWeight: 900, color: "#fff",
            boxShadow: "0 0 12px rgba(6,182,212,0.3)",
          }}>M</div>
          <div>
            <span style={{ fontSize: 14, fontWeight: 900, letterSpacing: "-0.3px" }}>MCS</span>
            <span style={{ fontSize: 9, color: "#475569", marginLeft: 8, letterSpacing: "1.5px", textTransform: "uppercase" }}>Network Operations</span>
          </div>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 16 }}>
          <span style={{ fontSize: 11, color: "#475569" }}>block-01 · Baldwinsville</span>
          <span style={{
            fontSize: 12, fontFamily: "monospace", fontWeight: 700, color: "#06b6d4",
            textShadow: "0 0 8px rgba(6,182,212,0.3)",
          }}>{new Date().toLocaleTimeString()}</span>
        </div>
      </div>

      {/* ── ALARM TICKER ── */}
      <AlarmTicker alarms={alarms} />

      {/* ── HERO METRICS BAR ── */}
      <div style={{
        display: "flex", borderBottom: "1px solid rgba(30,41,59,0.5)",
        background: "rgba(10,14,23,0.7)",
      }}>
        <HeroMetric label="IT Load" value={itLoad} unit="kW" range="1,000" color="#f59e0b" />
        <div style={{ width: 1, background: "rgba(30,41,59,0.5)" }} />
        <HeroMetric label="PUE" value={pue} unit="" color={pue < 1.12 ? "#10b981" : "#f59e0b"} />
        <div style={{ width: 1, background: "rgba(30,41,59,0.5)" }} />
        <HeroMetric label="Supply" value={supplyT} unit="°C" color="#06b6d4" />
        <div style={{ width: 1, background: "rgba(30,41,59,0.5)" }} />
        <HeroMetric label="Return" value={returnT} unit="°C" color="#ef4444" warn={returnT > 50} />
        <div style={{ width: 1, background: "rgba(30,41,59,0.5)" }} />
        <HeroMetric label="Heat Recovery" value={recovery} unit="%" color="#10b981" />
        <div style={{ width: 1, background: "rgba(30,41,59,0.5)" }} />
        <HeroMetric label="Host Water" value={hostOutT} unit="°C" color="#10b981" />
      </div>

      {/* ── MAIN CONTENT ── */}
      <div style={{ padding: 12, display: "grid", gridTemplateColumns: "1fr 340px", gap: 10 }}>

        {/* LEFT COLUMN */}
        <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>

          {/* Thermal Flow Diagram */}
          <div className="card card-glow" style={{ padding: 14 }}>
            <div className="scanline" />
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
              <span style={{ fontSize: 11, fontWeight: 700, color: "#64748b", letterSpacing: "1px", textTransform: "uppercase" }}>Thermal Loop Schematic</span>
              <span style={{
                fontSize: 10, fontWeight: 700, color: "#06b6d4", padding: "2px 8px",
                borderRadius: 10, border: "1px solid rgba(6,182,212,0.3)", background: "rgba(6,182,212,0.08)",
              }}>FULL_RECOVERY</span>
            </div>
            <ThermalFlowDiagram mode="FULL_RECOVERY" itLoad={Math.round(itLoad)} supplyTemp={supplyT} returnTemp={returnT} hostTemp={hostOutT} recoveryPct={Math.round(recovery)} />
          </div>

          {/* Trend Chart */}
          <div className="card" style={{ padding: 14 }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 6 }}>
              <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <span style={{ fontSize: 13, fontWeight: 800, fontFamily: "monospace", color: chartSensor.color }}>{chartSensor.tag}</span>
                <span style={{ fontSize: 11, color: "#475569" }}>{chartSensor.desc}</span>
              </div>
              <span style={{ fontSize: 20, fontWeight: 900, fontFamily: "monospace", color: chartSensor.color, textShadow: `0 0 10px ${chartSensor.color}44` }}>
                {chartSensor.val.toFixed(1)}<span style={{ fontSize: 11, color: "#475569" }}> {chartSensor.unit}</span>
              </span>
            </div>
            <ResponsiveContainer width="100%" height={160}>
              <AreaChart data={chartSensor.trend} margin={{ top: 5, right: 5, bottom: 5, left: 5 }}>
                <defs>
                  <linearGradient id="chartGrad" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor={chartSensor.color} stopOpacity={0.3} />
                    <stop offset="100%" stopColor={chartSensor.color} stopOpacity={0} />
                  </linearGradient>
                </defs>
                <XAxis dataKey="label" tick={{ fontSize: 8, fill: "#334155" }} axisLine={false} tickLine={false} interval={5} />
                <YAxis tick={{ fontSize: 8, fill: "#334155" }} axisLine={false} tickLine={false} width={30} domain={["auto", "auto"]} />
                <Tooltip contentStyle={{ background: "#111827", border: "1px solid #1e293b", borderRadius: 6, fontSize: 10, color: "#e2e8f0" }} />
                {chartSensor.warn && <ReferenceLine y={55} stroke="#ef4444" strokeDasharray="4 4" strokeOpacity={0.6} />}
                <Area type="monotone" dataKey="value" stroke={chartSensor.color} strokeWidth={2} fill="url(#chartGrad)" dot={false} />
              </AreaChart>
            </ResponsiveContainer>
          </div>

          {/* Rack Heatmap */}
          <div className="card" style={{ padding: 14 }}>
            <RackHeatmap racks={racks} />
          </div>
        </div>

        {/* RIGHT COLUMN — Gauges + Sensor List */}
        <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>

          {/* Radial Gauges */}
          <div className="card card-glow" style={{ padding: 12, display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 4 }}>
            <div className="scanline" />
            <RadialGauge value={pue} min={1.0} max={1.5} label="PUE" unit="" size={100} thresholds={{ yellow: 1.15, red: 1.3 }} />
            <RadialGauge value={recovery} min={0} max={100} label="Recovery" unit="%" size={100} color="#10b981" thresholds={{ yellow: 999, red: 999 }} />
            <RadialGauge value={itLoad / 10} min={0} max={100} label="Utilization" unit="%" size={100} color="#f59e0b" thresholds={{ yellow: 85, red: 95 }} />
          </div>

          {/* Sensor Table */}
          <div className="card" style={{ padding: 10, flex: 1, overflowY: "auto" }}>
            <div style={{ fontSize: 10, color: "#475569", letterSpacing: "1px", textTransform: "uppercase", marginBottom: 6, fontWeight: 700 }}>Live Sensors</div>
            {sensors.map(s => (
              <div key={s.tag} onClick={() => setSelectedChart(s.tag)} style={{
                display: "flex", alignItems: "center", gap: 6, padding: "5px 6px",
                borderRadius: 4, cursor: "pointer",
                background: selectedChart === s.tag ? "rgba(6,182,212,0.06)" : "transparent",
                borderLeft: selectedChart === s.tag ? "2px solid #06b6d4" : "2px solid transparent",
              }}
                onMouseEnter={e => { if (selectedChart !== s.tag) e.currentTarget.style.background = "rgba(255,255,255,0.02)"; }}
                onMouseLeave={e => { if (selectedChart !== s.tag) e.currentTarget.style.background = "transparent"; }}
              >
                <div style={{ width: 6, height: 6, borderRadius: "50%", background: s.warn ? "#ef4444" : "#10b981", boxShadow: s.warn ? "0 0 6px #ef4444" : "none" }} />
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontSize: 10, fontFamily: "monospace", color: "#94a3b8", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{s.tag}</div>
                </div>
                <Sparkline data={s.trend} width={50} height={18} color={s.color} />
                <div style={{ textAlign: "right", minWidth: 50 }}>
                  <span style={{
                    fontSize: 13, fontWeight: 800, fontFamily: "monospace",
                    color: s.warn ? "#ef4444" : s.color,
                    textShadow: s.warn ? "0 0 6px rgba(239,68,68,0.4)" : "none",
                  }}>{s.val.toFixed(1)}</span>
                  <span style={{ fontSize: 8, color: "#475569", marginLeft: 2 }}>{s.unit}</span>
                </div>
              </div>
            ))}
          </div>

          {/* Standing Alarms */}
          <div className="card" style={{ padding: 10 }}>
            <div style={{ fontSize: 10, color: "#475569", letterSpacing: "1px", textTransform: "uppercase", marginBottom: 6, fontWeight: 700 }}>
              Standing Alarms <span style={{ color: "#ef4444", marginLeft: 4 }}>({alarms.filter(a => a.state === "ACTIVE").length})</span>
            </div>
            {alarms.map(a => (
              <div key={a.id} style={{
                display: "flex", alignItems: "center", gap: 8, padding: "6px 8px",
                borderRadius: 6, marginBottom: 4,
                background: a.state === "ACTIVE" ? "rgba(239,68,68,0.06)" : "rgba(245,158,11,0.04)",
                borderLeft: `3px solid ${a.state === "ACTIVE" ? "#ef4444" : "#f59e0b"}`,
              }}>
                <span style={{
                  fontSize: 9, fontWeight: 800, padding: "1px 5px", borderRadius: 3,
                  background: a.priority === "P1" ? "#f97316" : "#f59e0b", color: "#000",
                }}>{a.priority}</span>
                <span style={{ fontSize: 11, fontFamily: "monospace", color: "#e2e8f0", flex: 1 }}>{a.tag}</span>
                <span style={{ fontSize: 12, fontFamily: "monospace", fontWeight: 800, color: a.state === "ACTIVE" ? "#ef4444" : "#f59e0b" }}>
                  {a.value} {a.dir} {a.threshold}
                </span>
                {a.state === "ACTIVE" && (
                  <button style={{
                    fontSize: 9, fontWeight: 800, padding: "2px 8px", borderRadius: 4,
                    border: "1px solid #10b981", background: "transparent", color: "#10b981",
                    cursor: "pointer", letterSpacing: "0.5px",
                  }}>ACK</button>
                )}
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
