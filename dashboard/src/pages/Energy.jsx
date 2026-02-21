import { useState, useMemo } from "react";
import { AreaChart, Area, BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid, Legend } from "recharts";

// ═══════════════════════════════════════════════════════════════════════════
// MCS STREAM D — ENERGY & HOST DASHBOARD
// ═══════════════════════════════════════════════════════════════════════════
// Shows electrical consumption, thermal recovery, PUE, and billing data.
// Used by both MicroLink ops and the host portal (read-only for hosts).

const C = {
  bg: "#0a0e17", card: "#111827", border: "#1e293b", text: "#e2e8f0",
  muted: "#64748b", dim: "#475569",
  blue: "#3b82f6", green: "#10b981", cyan: "#06b6d4",
  orange: "#f97316", yellow: "#f59e0b", red: "#ef4444", purple: "#8b5cf6",
};

// Generate 30 days of mock energy data
const generateEnergyData = () => {
  return Array.from({ length: 30 }, (_, i) => {
    const date = new Date(Date.now() - (29 - i) * 86400000);
    const baseLoad = 820 + Math.random() * 60;
    const pue = 1.08 + Math.random() * 0.04;
    const totalElectrical = baseLoad * pue;
    const thermalRecovered = baseLoad * (0.82 + Math.random() * 0.1);
    const thermalRejected = baseLoad * pue - baseLoad - thermalRecovered * 0.15;
    return {
      day: date.toLocaleDateString("en-US", { month: "short", day: "numeric" }),
      date: date.toISOString().slice(0, 10),
      electrical_kwh: Math.round(totalElectrical * 24),
      it_load_kwh: Math.round(baseLoad * 24),
      thermal_recovered: Math.round(thermalRecovered * 24),
      thermal_rejected: Math.round(Math.max(0, thermalRejected) * 24),
      heat_recovery_pct: Math.round((thermalRecovered / (totalElectrical - baseLoad + thermalRecovered)) * 100),
      pue: Number(pue.toFixed(3)),
      gas_displaced_therms: Math.round(thermalRecovered * 24 * 0.0341),
      co2_avoided_kg: Math.round(thermalRecovered * 24 * 0.0341 * 5.3),
    };
  });
};

const ENERGY_DATA = generateEnergyData();

const Metric = ({ label, value, unit, color, sub }) => (
  <div style={{ padding: "12px 14px", background: C.card, borderRadius: 8, border: `1px solid ${C.border}` }}>
    <div style={{ fontSize: 10, color: C.muted, textTransform: "uppercase", letterSpacing: "0.5px", marginBottom: 4 }}>{label}</div>
    <div style={{ fontSize: 26, fontWeight: 800, fontFamily: "'JetBrains Mono', monospace", color: color || C.text, lineHeight: 1.1 }}>
      {value}{unit && <span style={{ fontSize: 13, color: C.muted, marginLeft: 3 }}>{unit}</span>}
    </div>
    {sub && <div style={{ fontSize: 10, color: C.dim, marginTop: 2 }}>{sub}</div>}
  </div>
);

const CustomTooltip = ({ active, payload, label }) => {
  if (!active || !payload?.length) return null;
  return (
    <div style={{ background: C.card, border: `1px solid ${C.border}`, borderRadius: 8, padding: 10, fontSize: 11 }}>
      <div style={{ fontWeight: 700, marginBottom: 4 }}>{label}</div>
      {payload.map((p, i) => (
        <div key={i} style={{ color: p.color, marginBottom: 2 }}>
          {p.name}: <span style={{ fontFamily: "monospace", fontWeight: 700 }}>{typeof p.value === "number" ? p.value.toLocaleString() : p.value}</span>
        </div>
      ))}
    </div>
  );
};

export default function EnergyDashboard() {
  const [range, setRange] = useState(30);
  const data = useMemo(() => ENERGY_DATA.slice(-range), [range]);

  // Summary calculations
  const totals = useMemo(() => {
    const sum = (key) => data.reduce((s, d) => s + d[key], 0);
    const avg = (key) => sum(key) / data.length;
    return {
      totalElectricalMwh: (sum("electrical_kwh") / 1000).toFixed(1),
      totalITMwh: (sum("it_load_kwh") / 1000).toFixed(1),
      totalThermalMwh: (sum("thermal_recovered") / 1000).toFixed(1),
      avgPue: avg("pue").toFixed(3),
      avgRecovery: Math.round(avg("heat_recovery_pct")),
      totalCO2: Math.round(sum("co2_avoided_kg")),
      totalTherms: Math.round(sum("gas_displaced_therms")),
      estElectricalCost: Math.round(sum("electrical_kwh") * 0.065),
      estHeatCredit: Math.round(sum("thermal_recovered") * 0.025),
    };
  }, [data]);

  return (
    <div style={{ minHeight: "100vh", background: C.bg, color: C.text, fontFamily: "'DM Sans', system-ui, sans-serif", padding: 16 }}>
      <style>{`@import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;700;800&family=JetBrains+Mono:wght@400;700&display=swap');`}</style>

      {/* Header */}
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16 }}>
        <div>
          <div style={{ fontSize: 18, fontWeight: 800 }}>Energy & Heat Recovery</div>
          <div style={{ fontSize: 11, color: C.muted }}>block-01 · AB InBev Baldwinsville · {range}-day view</div>
        </div>
        <div style={{ display: "flex", gap: 4 }}>
          {[7, 14, 30].map(d => (
            <button key={d} onClick={() => setRange(d)} style={{
              padding: "5px 12px", borderRadius: 6,
              border: `1px solid ${range === d ? C.blue : C.border}`,
              background: range === d ? "rgba(59,130,246,0.15)" : "transparent",
              color: range === d ? C.blue : C.muted, fontSize: 11, fontWeight: 700, cursor: "pointer",
            }}>{d}d</button>
          ))}
        </div>
      </div>

      {/* KPI Cards */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(5, 1fr)", gap: 8, marginBottom: 16 }}>
        <Metric label="Total Electrical" value={totals.totalElectricalMwh} unit="MWh" color={C.blue} sub={`$${totals.estElectricalCost.toLocaleString()} @ $0.065/kWh`} />
        <Metric label="IT Load" value={totals.totalITMwh} unit="MWh" color={C.text} />
        <Metric label="Avg PUE" value={totals.avgPue} color={Number(totals.avgPue) < 1.15 ? C.green : C.yellow} sub="Target: < 1.15" />
        <Metric label="Heat Recovered" value={totals.totalThermalMwh} unit="MWh" color={C.cyan} sub={`$${totals.estHeatCredit.toLocaleString()} heat credit`} />
        <Metric label="Avg Recovery" value={`${totals.avgRecovery}`} unit="%" color={totals.avgRecovery > 80 ? C.green : C.yellow} sub="Target: > 80%" />
      </div>

      {/* Charts row */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12, marginBottom: 16 }}>
        {/* Electrical consumption */}
        <div style={{ background: C.card, borderRadius: 8, border: `1px solid ${C.border}`, padding: 14 }}>
          <div style={{ fontSize: 13, fontWeight: 700, marginBottom: 10 }}>Daily Electrical Consumption</div>
          <ResponsiveContainer width="100%" height={220}>
            <BarChart data={data} margin={{ top: 5, right: 5, bottom: 5, left: 5 }}>
              <CartesianGrid strokeDasharray="3 3" stroke={C.border} />
              <XAxis dataKey="day" tick={{ fontSize: 9, fill: C.dim }} axisLine={false} tickLine={false} interval={Math.floor(data.length / 6)} />
              <YAxis tick={{ fontSize: 9, fill: C.dim }} axisLine={false} tickLine={false} width={45} />
              <Tooltip content={<CustomTooltip />} />
              <Bar dataKey="it_load_kwh" name="IT Load (kWh)" fill={C.blue} radius={[2, 2, 0, 0]} stackId="a" />
              <Bar dataKey="electrical_kwh" name="Overhead (kWh)" fill="rgba(59,130,246,0.3)" radius={[2, 2, 0, 0]} stackId="b" />
            </BarChart>
          </ResponsiveContainer>
        </div>

        {/* Thermal recovery */}
        <div style={{ background: C.card, borderRadius: 8, border: `1px solid ${C.border}`, padding: 14 }}>
          <div style={{ fontSize: 13, fontWeight: 700, marginBottom: 10 }}>Thermal Energy Recovery</div>
          <ResponsiveContainer width="100%" height={220}>
            <AreaChart data={data} margin={{ top: 5, right: 5, bottom: 5, left: 5 }}>
              <defs>
                <linearGradient id="recoveredGrad" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor={C.cyan} stopOpacity={0.4} />
                  <stop offset="100%" stopColor={C.cyan} stopOpacity={0} />
                </linearGradient>
                <linearGradient id="rejectedGrad" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor={C.orange} stopOpacity={0.3} />
                  <stop offset="100%" stopColor={C.orange} stopOpacity={0} />
                </linearGradient>
              </defs>
              <CartesianGrid strokeDasharray="3 3" stroke={C.border} />
              <XAxis dataKey="day" tick={{ fontSize: 9, fill: C.dim }} axisLine={false} tickLine={false} interval={Math.floor(data.length / 6)} />
              <YAxis tick={{ fontSize: 9, fill: C.dim }} axisLine={false} tickLine={false} width={45} />
              <Tooltip content={<CustomTooltip />} />
              <Area type="monotone" dataKey="thermal_recovered" name="Recovered (kWh)" stroke={C.cyan} fill="url(#recoveredGrad)" strokeWidth={2} />
              <Area type="monotone" dataKey="thermal_rejected" name="Rejected (kWh)" stroke={C.orange} fill="url(#rejectedGrad)" strokeWidth={1.5} />
            </AreaChart>
          </ResponsiveContainer>
        </div>
      </div>

      {/* PUE + Recovery % trend */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12, marginBottom: 16 }}>
        <div style={{ background: C.card, borderRadius: 8, border: `1px solid ${C.border}`, padding: 14 }}>
          <div style={{ fontSize: 13, fontWeight: 700, marginBottom: 10 }}>PUE Trend</div>
          <ResponsiveContainer width="100%" height={180}>
            <AreaChart data={data} margin={{ top: 5, right: 5, bottom: 5, left: 5 }}>
              <defs>
                <linearGradient id="pueGrad" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor={C.green} stopOpacity={0.3} />
                  <stop offset="100%" stopColor={C.green} stopOpacity={0} />
                </linearGradient>
              </defs>
              <CartesianGrid strokeDasharray="3 3" stroke={C.border} />
              <XAxis dataKey="day" tick={{ fontSize: 9, fill: C.dim }} axisLine={false} tickLine={false} interval={Math.floor(data.length / 6)} />
              <YAxis domain={[1.0, 1.2]} tick={{ fontSize: 9, fill: C.dim }} axisLine={false} tickLine={false} width={35} />
              <Tooltip content={<CustomTooltip />} />
              <Area type="monotone" dataKey="pue" name="PUE" stroke={C.green} fill="url(#pueGrad)" strokeWidth={2} dot={false} />
            </AreaChart>
          </ResponsiveContainer>
        </div>

        {/* ESG impact */}
        <div style={{ background: C.card, borderRadius: 8, border: `1px solid ${C.border}`, padding: 14 }}>
          <div style={{ fontSize: 13, fontWeight: 700, marginBottom: 10 }}>Environmental Impact</div>
          <ResponsiveContainer width="100%" height={180}>
            <BarChart data={data} margin={{ top: 5, right: 5, bottom: 5, left: 5 }}>
              <CartesianGrid strokeDasharray="3 3" stroke={C.border} />
              <XAxis dataKey="day" tick={{ fontSize: 9, fill: C.dim }} axisLine={false} tickLine={false} interval={Math.floor(data.length / 6)} />
              <YAxis tick={{ fontSize: 9, fill: C.dim }} axisLine={false} tickLine={false} width={45} />
              <Tooltip content={<CustomTooltip />} />
              <Bar dataKey="co2_avoided_kg" name="CO₂ Avoided (kg)" fill={C.green} radius={[2, 2, 0, 0]} />
            </BarChart>
          </ResponsiveContainer>
        </div>
      </div>

      {/* Host value summary */}
      <div style={{ background: C.card, borderRadius: 8, border: `1px solid ${C.border}`, padding: 16 }}>
        <div style={{ fontSize: 14, fontWeight: 800, marginBottom: 12 }}>Host Value Summary — {range} Days</div>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 12 }}>
          <div>
            <div style={{ fontSize: 10, color: C.muted, textTransform: "uppercase", marginBottom: 4 }}>Gas Displaced</div>
            <div style={{ fontSize: 22, fontWeight: 800, fontFamily: "monospace", color: C.green }}>{totals.totalTherms.toLocaleString()}<span style={{ fontSize: 12, color: C.muted }}> therms</span></div>
          </div>
          <div>
            <div style={{ fontSize: 10, color: C.muted, textTransform: "uppercase", marginBottom: 4 }}>CO₂ Avoided</div>
            <div style={{ fontSize: 22, fontWeight: 800, fontFamily: "monospace", color: C.green }}>{(totals.totalCO2 / 1000).toFixed(1)}<span style={{ fontSize: 12, color: C.muted }}> tonnes</span></div>
          </div>
          <div>
            <div style={{ fontSize: 10, color: C.muted, textTransform: "uppercase", marginBottom: 4 }}>Host Gas Savings</div>
            <div style={{ fontSize: 22, fontWeight: 800, fontFamily: "monospace", color: C.cyan }}>${(totals.totalTherms * 1.1).toLocaleString()}<span style={{ fontSize: 12, color: C.muted }}> est.</span></div>
          </div>
          <div>
            <div style={{ fontSize: 10, color: C.muted, textTransform: "uppercase", marginBottom: 4 }}>Heat Credit to Host</div>
            <div style={{ fontSize: 22, fontWeight: 800, fontFamily: "monospace", color: C.purple }}>${totals.estHeatCredit.toLocaleString()}<span style={{ fontSize: 12, color: C.muted }}> @ $0.025/kWh</span></div>
          </div>
        </div>
      </div>
    </div>
  );
}
