import { useEffect, useRef, useState } from "react";
import ChartController from "../chart";
import { api, fmt } from "../api";
import useStore from "../store";

const TIMEFRAMES = ["1m", "5m", "15m", "30m", "1h", "4h", "12h", "1d", "1w"];

export default function ChartPanel({ source }) {
  source = useStore(s => s.chartSource); // reactive — App's prop is read once at mount
  const containerRef = useRef(null);
  const ctrlRef = useRef(null);
  const symbol = useStore(s => s.symbol);
  const res = useStore(s => s.res);
  const setRes = useStore(s => s.setRes);
  const instruments = useStore(s => s.instruments);
  const bindChart = useStore(s => s.bindChart);
  const unbindChart = useStore(s => s.unbindChart);
  const [note, setNote] = useState("");
  const [signal, setSignal] = useState(null);
  const [riskEnabled, setRiskEnabled] = useState(() => localStorage.getItem("kt.riskEnabled") === "1");

  useEffect(() => {
    const ctrl = new ChartController();
    ctrlRef.current = ctrl;
    bindChart(ctrl);
    ctrl.mount(containerRef.current, useStore.getState().symbol, useStore.getState().instruments);
    return () => { ctrl.destroy(); unbindChart(); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const ctrl = ctrlRef.current;
    if (ctrl) ctrl.applyPriceFormat(symbol, instruments);
    api(`/api/signal?symbol=${encodeURIComponent(symbol)}`)
      .then(s => setSignal(s.error ? null : s))
      .catch(() => setSignal(null));
  }, [symbol, instruments]);

  useEffect(() => { ctrlRef.current?.setRiskEnabled(riskEnabled); }, [riskEnabled]);

  const toggleRisk = () => {
    const enabled = !riskEnabled;
    localStorage.setItem("kt.riskEnabled", enabled ? "1" : "0");
    setRiskEnabled(enabled);
  };
  const side = signal && signal.side ? signal.side : "";

  return (
    <div id="chart-panel">
      <div className="tf-bar" id="tf-bar">
        {TIMEFRAMES.map(r => (
          <button key={r} className={"tf-btn" + (r === res ? " active" : "")} onClick={() => setRes(r)}>{r}</button>
        ))}
        <button
          className={"risk-toggle" + (riskEnabled ? " active" : "")}
          aria-pressed={riskEnabled}
          title="Show 1:1, 1:2, and 1:3 mirrored loss lines for the take profit"
          onClick={toggleRisk}
        >
          RISK {riskEnabled ? "ON" : "OFF"}
        </button>
        <span
          className={"ema-badge" + (side ? " " + side : "")}
          title={signal && signal.price !== undefined ? `price ${fmt(signal.price)} · ema${signal.fast} ${fmt(signal.emaFast)} · ema${signal.slow} ${fmt(signal.emaSlow)}` : "EMA trend signal on closed 1m mark candles"}
        >
          {signal && !signal.error ? `EMA${signal.fast}/${signal.slow}: ${side ? side.toUpperCase() : "flat"}` : "EMA –"}
        </span>
      </div>
      <div id="chart" ref={containerRef}></div>
      <div id="chart-note">{note || (source === "hub" ? "built from live trades" : source === "rest" ? "Kraken charts API" : source === "binance" ? "Binance USDT-M klines" : "")}</div>
    </div>
  );
}
