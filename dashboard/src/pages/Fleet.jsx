import { useState } from "react";

// ═══════════════════════════════════════════════════════════════════════════
// MCS STREAM D — SITE & FLEET OVERVIEW
// ═══════════════════════════════════════════════════════════════════════════
// Top-level navigation: all sites → blocks → drill into NOC dashboard.

const C = {
  bg: "#0a0e17", card: "#111827", cardHover: "#1a2234", border: "#1e293b",
  text: "#e2e8f0", muted: "#64748b", dim: "#475569",
  blue: "#3b82f6", green: "#10b981", cyan: "#06b6d4",
  yellow: "#f59e0b", red: "#ef4444", orange: "#f97316",
};

const MOCK_SITES = [
  {
    slug: "baldwinsville", name: "AB InBev Baldwinsville", region: "US-East",
    status: "active", lat: 43.16, lng: -76.33,
    blocks: [
      { slug: "block-01", capacity_mw: 1.0, status: "active", thermal_mode: "FULL_RECOVERY", it_load_kw: 847, pue: 1.09, heat_recovery: 0.87, sensors: 312, alarms_standing: 2 },
    ],
  },
  {
    slug: "jackblack-cpt", name: "Jack Black Brewery", region: "ZA-WC",
    status: "commissioning", lat: -33.92, lng: 18.42,
    blocks: [
      { slug: "jb-block-01", capacity_mw: 0.5, status: "commissioning", thermal_mode: "STARTUP", it_load_kw: 0, pue: null, heat_recovery: null, sensors: 156, alarms_standing: 0 },
    ],
  },
  {
    slug: "rfg-foods", name: "RFG Foods Krugersdorp", region: "ZA-GP",
    status: "commissioning", lat: -26.10, lng: 27.77,
    blocks: [
      { slug: "rfg-block-01", capacity_mw: 1.0, status: "commissioning", thermal_mode: "STARTUP", it_load_kw: 0, pue: null, heat_recovery: null, sensors: 298, alarms_standing: 0 },
    ],
  },
];

const Badge = ({ children, color, bg }) => (
  <span style={{ fontSize: 10, fontWeight: 700, letterSpacing: "0.5px", padding: "2px 7px", borderRadius: 4, color, backgroundColor: bg, whiteSpace: "nowrap" }}>{children}</span>
);

const StatusIndicator = ({ status }) => {
  const colors = { active: C.green, commissioning: C.yellow, standby: C.orange, decommissioned: C.dim };
  return <Badge color="#fff" bg={colors[status] || C.dim}>{status.toUpperCase()}</Badge>;
};

const BlockCard = ({ block, onSelect }) => {
  const hasAlarms = block.alarms_standing > 0;
  return (
    <div onClick={() => onSelect(block)} style={{
      background: C.card, borderRadius: 8, border: `1px solid ${hasAlarms ? C.red : C.border}`,
      padding: 14, cursor: "pointer", transition: "background 0.15s",
      boxShadow: hasAlarms ? `0 0 12px ${C.red}22` : "none",
    }}
      onMouseEnter={e => e.currentTarget.style.background = C.cardHover}
      onMouseLeave={e => e.currentTarget.style.background = C.card}
    >
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
        <span style={{ fontSize: 14, fontWeight: 700, fontFamily: "monospace" }}>{block.slug}</span>
        <StatusIndicator status={block.status} />
      </div>
      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 8 }}>
        <span style={{ fontSize: 11, color: C.muted }}>{block.capacity_mw} MW</span>
        <span style={{ fontSize: 11, color: C.muted }}>{block.sensors} sensors</span>
      </div>
      {block.status === "active" && (
        <>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 8, marginBottom: 8 }}>
            <div>
              <div style={{ fontSize: 9, color: C.muted, textTransform: "uppercase" }}>IT Load</div>
              <div style={{ fontSize: 16, fontWeight: 800, fontFamily: "monospace" }}>{block.it_load_kw}<span style={{ fontSize: 10, color: C.muted }}> kW</span></div>
            </div>
            <div>
              <div style={{ fontSize: 9, color: C.muted, textTransform: "uppercase" }}>PUE</div>
              <div style={{ fontSize: 16, fontWeight: 800, fontFamily: "monospace", color: block.pue < 1.15 ? C.green : C.yellow }}>{block.pue}</div>
            </div>
            <div>
              <div style={{ fontSize: 9, color: C.muted, textTransform: "uppercase" }}>Recovery</div>
              <div style={{ fontSize: 16, fontWeight: 800, fontFamily: "monospace", color: C.cyan }}>{Math.round(block.heat_recovery * 100)}%</div>
            </div>
          </div>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <Badge color={C.cyan} bg="rgba(6,182,212,0.12)">{block.thermal_mode}</Badge>
            {hasAlarms && <Badge color="#fff" bg={C.red}>{block.alarms_standing} STANDING</Badge>}
          </div>
        </>
      )}
      {block.status === "commissioning" && (
        <div style={{ fontSize: 11, color: C.yellow, fontStyle: "italic" }}>Commissioning in progress...</div>
      )}
    </div>
  );
};

const SiteCard = ({ site, onSelectBlock }) => (
  <div style={{ background: C.card, borderRadius: 10, border: `1px solid ${C.border}`, overflow: "hidden", marginBottom: 12 }}>
    <div style={{ padding: "14px 16px", borderBottom: `1px solid ${C.border}` }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <div>
          <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 2 }}>
            <span style={{ fontSize: 16, fontWeight: 800 }}>{site.name}</span>
            <StatusIndicator status={site.status} />
          </div>
          <div style={{ fontSize: 11, color: C.muted }}>{site.slug} · {site.region}</div>
        </div>
        <div style={{ fontSize: 11, color: C.dim }}>
          {site.blocks.length} block{site.blocks.length !== 1 ? "s" : ""} ·
          {site.blocks.reduce((s, b) => s + b.capacity_mw, 0)} MW
        </div>
      </div>
    </div>
    <div style={{ padding: 12, display: "grid", gridTemplateColumns: `repeat(${Math.min(site.blocks.length, 3)}, 1fr)`, gap: 8 }}>
      {site.blocks.map(b => <BlockCard key={b.slug} block={b} onSelect={onSelectBlock} />)}
    </div>
  </div>
);

export default function FleetOverview() {
  const totalMW = MOCK_SITES.reduce((s, site) => s + site.blocks.reduce((s2, b) => s2 + b.capacity_mw, 0), 0);
  const activeMW = MOCK_SITES.reduce((s, site) => s + site.blocks.filter(b => b.status === "active").reduce((s2, b) => s2 + b.capacity_mw, 0), 0);
  const totalBlocks = MOCK_SITES.reduce((s, site) => s + site.blocks.length, 0);
  const totalStanding = MOCK_SITES.reduce((s, site) => s + site.blocks.reduce((s2, b) => s2 + b.alarms_standing, 0), 0);

  const handleSelectBlock = (block) => {
    // In production: navigate to /dashboard/{block.slug}
    alert(`Navigate to NOC dashboard for ${block.slug}`);
  };

  return (
    <div style={{ minHeight: "100vh", background: C.bg, color: C.text, fontFamily: "'DM Sans', system-ui, sans-serif", padding: 16 }}>
      <style>{`@import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;700;800&family=JetBrains+Mono:wght@400;700&display=swap');`}</style>

      {/* Header */}
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <div style={{
            width: 36, height: 36, borderRadius: 8, background: "linear-gradient(135deg, #3b82f6, #06b6d4)",
            display: "flex", alignItems: "center", justifyContent: "center",
            fontSize: 18, fontWeight: 900, color: "#fff",
          }}>M</div>
          <div>
            <div style={{ fontSize: 18, fontWeight: 800, letterSpacing: "-0.3px" }}>MicroLink Fleet</div>
            <div style={{ fontSize: 10, color: C.muted, letterSpacing: "0.5px", textTransform: "uppercase" }}>Network Operations Center</div>
          </div>
        </div>
      </div>

      {/* Fleet KPIs */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(5, 1fr)", gap: 8, marginBottom: 16 }}>
        {[
          { label: "Sites", value: MOCK_SITES.length, color: C.text },
          { label: "Blocks", value: totalBlocks, color: C.text },
          { label: "Deployed MW", value: `${totalMW}`, color: C.blue },
          { label: "Active MW", value: `${activeMW}`, color: C.green },
          { label: "Standing Alarms", value: totalStanding, color: totalStanding > 0 ? C.red : C.green },
        ].map(m => (
          <div key={m.label} style={{ background: C.card, borderRadius: 8, border: `1px solid ${C.border}`, padding: "10px 14px" }}>
            <div style={{ fontSize: 10, color: C.muted, textTransform: "uppercase", letterSpacing: "0.5px", marginBottom: 2 }}>{m.label}</div>
            <div style={{ fontSize: 28, fontWeight: 800, fontFamily: "monospace", color: m.color }}>{m.value}</div>
          </div>
        ))}
      </div>

      {/* Site list */}
      {MOCK_SITES.map(site => (
        <SiteCard key={site.slug} site={site} onSelectBlock={handleSelectBlock} />
      ))}
    </div>
  );
}
