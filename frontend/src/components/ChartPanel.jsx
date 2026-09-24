import { useEffect, useRef, useState } from "react";
import { VelaWorkspace } from "@luxalgo/vela/workspace";
import { api, fmt } from "../api";
import { READ_ONLY, venueKey, isVenueSymbol } from "../exchange.js";
import useStore from "../store";
import { VelaChartController } from "../vela-overlays";
import { TerminalVelaProvider, toTerminalTimeframe, toVelaTimeframe, VELA_TIMEFRAMES } from "../vela-provider";

export default function ChartPanel() {
  const containerRef = useRef(null);
  const controllerRef = useRef(null);
  const bindChart = useStore(state => state.bindChart);
  const unbindChart = useStore(state => state.unbindChart);
  const symbol = useStore(state => state.symbol);
  const [signal, setSignal] = useState(null);
  const [note, setNote] = useState("");
  // On Hyperliquid, say why TP/SL lines cannot be dragged while a position is open.
  const dragBlocked = useStore(state => state.exchange === "hyperliquid" &&
    state.positions.some(p => !p.error && p.symbol === state.symbol) ? state.hlChartBlockReason() : null);
  const [riskEnabled, setRiskEnabled] = useState(() => localStorage.getItem(venueKey("kt.riskEnabled")) === "1");

  useEffect(() => {
    const initial = useStore.getState();
    const workspace = new VelaWorkspace(containerRef.current, {
      layout: "1",
      symbol: initial.symbol,
      timeframe: toVelaTimeframe(initial.res),
      cells: {
        primary: { symbol: initial.symbol, timeframe: toVelaTimeframe(initial.res) },
      },
      providers: { terminal: () => new TerminalVelaProvider() },
      timeframes: VELA_TIMEFRAMES,
      live: true,
      theme: "dark",
      persist: venueKey("terminal-vela-workspace"),
      drawings: false,
      drawingToolbar: false,
      bottombar: false,
      topbar: {
        left: ["symbol", "timeframes", "style", "layout"],
        right: ["screenshot"],
      },
    });
    const controller = new VelaChartController(workspace);
    controllerRef.current = controller;
    bindChart(controller);
    controller.setRiskEnabled(localStorage.getItem(venueKey("kt.riskEnabled")) === "1");
    window.__velaWorkspace = workspace;

    const syncActiveMarket = () => {
      const market = workspace.chart.market;
      const nextSymbol = String(market.symbol || "").toUpperCase();
      if (!isVenueSymbol(nextSymbol)) return;
      const res = toTerminalTimeframe(market.timeframe);
      const current = useStore.getState();
      useStore.setState({ symbol: nextSymbol, res, prevPrice: null });
      localStorage.setItem(venueKey("kt.symbol"), nextSymbol);
      localStorage.setItem(venueKey("kt.res"), res);
      api(`/api/tickers?symbols=${encodeURIComponent(nextSymbol)}`).catch(() => {});
      current.applyOverlayLines();
      current.refreshBook?.();
      current.refreshSignal();
    };

    const cellSubscriptions = new Map();
    const bindCell = cell => {
      if (!cell || cellSubscriptions.has(cell.id)) return;
      cellSubscriptions.set(cell.id, cell.chart.on("market:changed", () => {
        if (workspace.getState().activeCellId === cell.id) syncActiveMarket();
        else useStore.getState().applyOverlayLines();
      }));
    };
    workspace.cells().forEach(bindCell);
    const offActive = workspace.on("cell:active", syncActiveMarket);
    const offCreated = workspace.on("cell:created", ({ id }) => {
      bindCell(workspace.cell(id));
      useStore.getState().applyOverlayLines();
    });
    const offDestroyed = workspace.on("cell:destroyed", ({ id }) => {
      cellSubscriptions.get(id)?.();
      cellSubscriptions.delete(id);
    });

    return () => {
      offActive();
      offCreated();
      offDestroyed();
      for (const unsubscribe of cellSubscriptions.values()) unsubscribe();
      unbindChart();
      controller.destroy();
      workspace.destroy();
      controllerRef.current = null;
      delete window.__velaWorkspace;
    };
  }, [bindChart, unbindChart]);

  useEffect(() => {
    if (READ_ONLY) return;
    api(`/api/signal?symbol=${encodeURIComponent(symbol)}`)
      .then(result => setSignal(result.error ? null : result))
      .catch(() => setSignal(null));
  }, [symbol]);

  useEffect(() => { controllerRef.current?.setRiskEnabled(riskEnabled); }, [riskEnabled]);

  const toggleRisk = () => {
    const enabled = !riskEnabled;
    localStorage.setItem(venueKey("kt.riskEnabled"), enabled ? "1" : "0");
    setRiskEnabled(enabled);
    setNote("");
  };

  const fitRisk = () => {
    setNote(controllerRef.current?.fitRisk() ? "" : "No visible TP risk levels to fit");
  };

  const side = signal?.side || "";
  return (
    <div id="chart-panel" className="vela-chart-panel">
      <div className="vela-terminal-tools">
        <button
          className={"risk-toggle" + (riskEnabled ? " active" : "")}
          aria-pressed={riskEnabled}
          title="Show 1:1, 1:2, and 1:3 mirrored loss lines for the active chart"
          onClick={toggleRisk}
        >
          RISK {riskEnabled ? "ON" : "OFF"}
        </button>
        {riskEnabled && (
          <button className="risk-toggle" title="Fit the active chart to its mirrored risk levels" onClick={fitRisk}>
            FIT RISK
          </button>
        )}
        <span
          className={"ema-badge" + (side ? ` ${side}` : "")}
          title={signal?.price !== undefined ? `price ${fmt(signal.price)} · ema${signal.fast} ${fmt(signal.emaFast)} · ema${signal.slow} ${fmt(signal.emaSlow)}` : "EMA trend signal on closed 1m mark candles"}
        >
          {signal && !signal.error ? `EMA${signal.fast}/${signal.slow}: ${side ? side.toUpperCase() : "flat"}` : "EMA –"}
        </span>
      </div>
      <div className={"vela-status-badge" + (dragBlocked && !note ? " warn" : "")}>
        {note || (dragBlocked ? `TP/SL DRAG OFF: ${dragBlocked}` : "VELA · ACTIVE CHART DRIVES TERMINAL")}
      </div>
      <div id="vela-chart" ref={containerRef}></div>
    </div>
  );
}
