import { Routes, Route, NavLink } from 'react-router-dom';
import Dashboard from './pages/Dashboard';
import Fleet from './pages/Fleet';
import Alarms from './pages/Alarms';
import Energy from './pages/Energy';

const navStyle = {
  display: 'flex', gap: 2, padding: '6px 12px',
  borderBottom: '1px solid rgba(30,41,59,0.5)',
  background: 'rgba(10,14,23,0.95)',
  fontFamily: "'DM Sans', system-ui, sans-serif",
};

const linkStyle = (isActive) => ({
  padding: '6px 14px', borderRadius: 6, fontSize: 11, fontWeight: 700,
  textDecoration: 'none', letterSpacing: '0.5px', textTransform: 'uppercase',
  color: isActive ? '#06b6d4' : '#64748b',
  background: isActive ? 'rgba(6,182,212,0.08)' : 'transparent',
  border: `1px solid ${isActive ? 'rgba(6,182,212,0.3)' : 'transparent'}`,
});

export default function App() {
  return (
    <div style={{ minHeight: '100vh', background: '#060a12' }}>
      <nav style={navStyle}>
        <NavLink to="/" style={({ isActive }) => linkStyle(isActive)} end>Fleet</NavLink>
        <NavLink to="/dashboard" style={({ isActive }) => linkStyle(isActive)}>NOC Dashboard</NavLink>
        <NavLink to="/alarms" style={({ isActive }) => linkStyle(isActive)}>Alarms</NavLink>
        <NavLink to="/energy" style={({ isActive }) => linkStyle(isActive)}>Energy</NavLink>
      </nav>
      <Routes>
        <Route path="/" element={<Fleet />} />
        <Route path="/dashboard" element={<Dashboard />} />
        <Route path="/dashboard/:blockSlug" element={<Dashboard />} />
        <Route path="/alarms" element={<Alarms />} />
        <Route path="/energy" element={<Energy />} />
      </Routes>
    </div>
  );
}
