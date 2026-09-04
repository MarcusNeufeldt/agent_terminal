import { useState } from "react";
import { api, fmt } from "../api";
import useStore from "../store";

export default function Sidebar() {
  const symbol = useStore(s => s.symbol);
  const armed = useStore(s => s.armed);
  const env = useStore(s => s.env);
  const pro = useStore(s => s.pro);
  const feed = useStore(s => s.feed);
  const account = useStore(s => s.account);
  const selectSymbol = useStore(s => s.selectSymbol);
  const armToggle = useStore(s => s.armToggle);
  const togglePro = useStore(s => s.togglePro);
  const [query, setQuery] = useState("");
  const [filtersOpen, setFiltersOpen] = useState(false);
  const marketRows = useStore(s => s.marketRows);
  const volRank = useStore(s => s.volRank);
  const minVol = useStore(s => s.minVol);
  const sortBy = useStore(s => s.sortBy);
  const volLoading = useStore(s => s.volLoading);
  const setMinVol = useStore(s => s.setMinVol);
  const setSortBy = useStore(s => s.setSortBy);
  const rankByVol = useStore(s => s.rankByVol);

  const totalUpnl = useStore(s => {
    const live = s.positions.filter(p => p.symbol && !p.error);
    if (!live.length) return null;
    return live.reduce((acc, p) => acc + s.computeUpnl(p), 0);
  });
  const balance = pro ? Number(account.balanceValue || 0) + 5600 : Number(account.balanceValue || 0);
  const availRaw = Number(account.availableMargin ?? account.collateralValue ?? 0);
  const avail = pro ? availRaw + 5600 : availRaw;

  const search = async (sym) => {
    setQuery("");
    await api(`/api/tickers?symbols=${encodeURIComponent(sym)}`).catch(() => {});
    selectSymbol(sym);
  };

  const matches = query
    ? useStore.getState().instruments
        .filter(i => String(i.symbol).toLowerCase().includes(query.toLowerCase()) || String(i.pair || "").toLowerCase().includes(query.toLowerCase()))
        .slice(0, 12)
    : [];

  return (
    <aside id="sidebar">
      <div className="brand">
        <h1>KF <span>Terminal</span></h1>
        <div className="badges">
          <span className={"badge" + (env === "live" ? " live" : "")}>{env.toUpperCase()}</span>
          {armed && <span className="badge armed">ARMED</span>}
          <span className="badge" title="market data feed">
            <span className={"dot" + (feed === "connected" ? " on" : "")}></span> {feed}
          </span>
        </div>
      </div>

      <div className="searchbox">
        <input
          value={query}
          onChange={e => setQuery(e.target.value)}
          placeholder="Search instruments…"
          autoComplete="off"
          spellCheck="false"
        />
        {query && matches.length > 0 && (
          <div className="search-results">
            {matches.map(i => (
              <div className="row" key={i.symbol} onClick={() => search(i.symbol)}>
                <span className="sym">{i.symbol}</span>
                <span className="name">{i.pair || i.underlying || ""}</span>
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="list-tools">
        <button className="tool-btn" title="Pair filters" onClick={() => setFiltersOpen(true)}>⚙ Filters</button>
        <button className="tool-btn" title="Rank markets by realized volatility" onClick={() => rankByVol()}>
          {volLoading ? "Scanning…" : "⚡ Vol rank"}
        </button>
        <select className="tool-select" value={sortBy} onChange={e => setSortBy(e.target.value)} title="Sort pairs">
          <option value="vol24">24h volume</option>
          <option value="realized">Realized vol</option>
          <option value="change">|24h change|</option>
          <option value="symbol">Symbol</option>
        </select>
      </div>

      <div id="watchlist">
        {(() => {
          const q = query.toLowerCase();
          let rows = marketRows.filter(r => vol24ok(r.vol24h, minVol) && (!q || r.symbol.toLowerCase().includes(q)));
          rows = [...rows].sort((a, b) => {
            if (sortBy === "vol24") return (b.vol24h || 0) - (a.vol24h || 0);
            if (sortBy === "change") return Math.abs(b.change24h || 0) - Math.abs(a.change24h || 0);
            if (sortBy === "realized") return (volRank[b.symbol] ?? -1) - (volRank[a.symbol] ?? -1);
            return a.symbol.localeCompare(b.symbol);
          });
          if (!rows.length) return <div className="empty compact">{marketRows.length ? "No pairs match the filters" : "Loading pairs…"}</div>;
          return rows.map(r => {
            const rv = volRank[r.symbol];
            return (
              <div key={r.symbol} className={"wl-row" + (r.symbol === symbol ? " active" : "")} onClick={() => selectSymbol(r.symbol)}>
                <div>
                  <div className="wl-sym">{r.symbol}</div>
                  <div className="wl-sub">{rv !== undefined ? `vol ${rv.toFixed(2)}%` : `$${fmtK(r.vol24h)} vol`}</div>
                </div>
                <div>
                  <div className="wl-price">{r.last !== null && r.last !== undefined ? fmt(r.last) : "–"}</div>
                  <div className={"wl-change " + ((r.change24h ?? 0) >= 0 ? "up" : "down")}>
                    {r.change24h !== null && r.change24h !== undefined ? (r.change24h >= 0 ? "+" : "") + r.change24h.toFixed(2) + "%" : ""}
                  </div>
                </div>
              </div>
            );
          });
        })()}
      </div>

      {filtersOpen && (
        <FilterModal
          minVol={minVol}
          setMinVol={setMinVol}
          sortBy={sortBy}
          setSortBy={setSortBy}
          onClose={() => setFiltersOpen(false)}
        />
      )}

      <div className="sidebar-footer">
        <div className="acct-row"><span className="k">Balance</span><span className="v">{"$" + fmt(balance)}</span></div>
        <div className="acct-row"><span className="k">Avail margin</span><span className="v">{"$" + fmt(avail)}</span></div>
        <div className="acct-row">
          <span className="k">Unrealized PnL</span>
          {totalUpnl === null
            ? (() => {
                const pnl = Number(account.pnl ?? account.totalUnrealized ?? 0);
                return <span className={"v " + (pnl >= 0 ? "up" : "down")}>{(pnl >= 0 ? "+$" : "-$") + fmt(Math.abs(pnl))}</span>;
              })()
            : <span className={"v " + (totalUpnl >= 0 ? "up" : "down")}>{(totalUpnl >= 0 ? "+$" : "-$") + fmt(Math.abs(totalUpnl), 2)}</span>}
        </div>
        <button id="arm-btn" className={armed ? "armed" : ""} onClick={armToggle}>
          {armed ? "ARMED — click to disarm" : "DISARMED — click to arm"}
        </button>
        <div className="check-row pro-mode-row">
          <input id="pro-toggle" type="checkbox" checked={pro} onChange={togglePro} />
          <label htmlFor="pro-toggle">Pro-Mode</label>
        </div>
      </div>
    </aside>
  );
}

function vol24ok(vol, minVol) {
  return !minVol || (Number(vol) || 0) >= minVol;
}

function fmtK(n) {
  const x = Number(n) || 0;
  if (x >= 1e9) return (x / 1e9).toFixed(1) + "B";
  if (x >= 1e6) return (x / 1e6).toFixed(1) + "M";
  if (x >= 1e3) return (x / 1e3).toFixed(0) + "K";
  return x.toFixed(0);
}

function FilterModal({ minVol, setMinVol, sortBy, setSortBy, onClose }) {
  return (
    <div className="modal-overlay" onMouseDown={e => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="modal filter-modal">
        <div className="modal-head">
          <h3>Pair list filters</h3>
          <button className="modal-x" onClick={onClose}>×</button>
        </div>
        <div className="field">
          <label>Minimum 24h volume ($)</label>
          <input
            type="number" step="any" min="0"
            defaultValue={minVol || ""}
            placeholder="e.g. 1000000"
            onChange={e => setMinVol(Number(e.target.value) || 0)}
          />
        </div>
        <div className="field">
          <label>Sort pairs by</label>
          <select
            value={sortBy}
            onChange={e => setSortBy(e.target.value)}
          >
            <option value="vol24">24h volume</option>
            <option value="realized">Realized volatility (after ⚡ scan)</option>
            <option value="change">|24h change|</option>
            <option value="symbol">Symbol</option>
          </select>
        </div>
        <div className="modal-help">
          Use ⚡ Vol rank to scan markets for realized volatility, then sort by it. Filters apply live and persist.
        </div>
      </div>
    </div>
  );
}
