import { useState } from "react";
import { api, fmt } from "../api";
import useStore from "../store";
import RulesPanel from "./RulesPanel";

export default function Sidebar() {
  const symbol = useStore(s => s.symbol);
  const exchange = useStore(s => s.exchange);
  const exchangeName = useStore(s => s.exchangeName);
  const exchangeRouting = useStore(s => s.exchangeRouting);
  const exchangeBusy = useStore(s => s.exchangeBusy);
  const switchExchange = useStore(s => s.switchExchange);
  const readOnly = useStore(s => s.readOnly);
  const canTrade = useStore(s => s.canTrade);
  const armed = useStore(s => s.armed);
  const env = useStore(s => s.env);
  const pro = useStore(s => s.pro);
  const proAdj = useStore(s => s.proAdj);
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

  const totalUpnl = useStore(s => s.totalUpnl());
  const balanceRaw = account.balanceValue == null ? null : Number(account.balanceValue);
  const available = readOnly ? account.withdrawable : account.availableMargin ?? account.collateralValue;
  const availRaw = available == null ? null : Number(available);
  const balance = Number.isFinite(balanceRaw) ? proAdj(balanceRaw) : null;
  const avail = Number.isFinite(availRaw) ? proAdj(availRaw) : null;
  const spot = account.spotUsdc == null ? null : Number(account.spotUsdc);
  const unified = account.unified === true;

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
        <h1>{readOnly ? "HL" : "KF"} <span>Terminal</span></h1>
        <label className="exchange-picker" htmlFor="exchange-select">
          Exchange
          <select id="exchange-select" value={exchange} disabled={!exchangeRouting || exchangeBusy}
            title={exchangeRouting ? "Switch venue and disarm the terminal" : "Restart the updated backend to enable exchange switching"}
            onChange={e => switchExchange(e.target.value)}>
            <option value="kraken">Kraken Futures</option>
            <option value="hyperliquid">Hyperliquid · Read-only</option>
          </select>
        </label>
        <div className="badges">
          {readOnly && !canTrade && <span className="badge">READ-ONLY</span>}
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
                  <div className="wl-price" title={readOnly ? "Hyperliquid mark price; header and position PnL use actual last trades" : "Last trade"}>{fmt(readOnly ? r.markPrice : r.last)}</div>
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
        <div className="acct-row"><span className="k" title={unified ? "Unified account: one USDC balance collateralises both spot and perps, so no transfer is needed" : undefined}>{readOnly ? (unified ? "USDC balance" : "Perp balance") : "Balance"}</span><span className="v">{balance === null ? "–" : "$" + fmt(balance)}</span></div>
        {readOnly && !unified && <div className="acct-row"><span className="k" title="Separate spot balance; perp orders need funds moved to perp">Spot USDC</span><span className="v">{Number.isFinite(spot) ? "$" + fmt(spot) : "–"}</span></div>}
        {!(readOnly && avail === null) && <div className="acct-row"><span className="k">{readOnly ? "Perp withdrawable" : "Avail margin"}</span><span className="v">{avail === null ? "–" : "$" + fmt(avail)}</span></div>}
        <div className="acct-row">
          <span className="k" title={`What closing every position at market on ${exchangeName} would add right now: the order book walked for each full position, less the taker fee${exchangeName === "Kraken" ? ", plus funding settled on close" : ""}, and less the entry fee already paid (estimated at the taker rate). Without a fresh book it uses the best bid/ask.`}>Net if closed</span>
          {totalUpnl === null
            ? <span className="v muted" title="Book PnL unavailable until valid position and quote data arrive">{fmt(null)}</span>
            : <span className={"v " + (totalUpnl >= 0 ? "up" : "down")}>{(totalUpnl >= 0 ? "+$" : "-$") + fmt(Math.abs(totalUpnl), 2)}</span>}
        </div>
        {!readOnly && <RulesPanel />}
        <button id="arm-btn" disabled={(!canTrade && !armed) || exchangeBusy} className={armed ? "armed" : ""} onClick={armToggle}>
          {!canTrade && !armed ? "READ-ONLY · Trading unavailable" : armed ? "ARMED — click to disarm" : "DISARMED — click to arm"}
        </button>
        <div className="check-row pro-mode-row">
          <input id="pro-toggle" type="checkbox" disabled={readOnly} checked={pro} onChange={togglePro} />
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
