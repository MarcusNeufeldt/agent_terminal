/* Shared order-ticket controls for the Kraken and Hyperliquid tickets. */

export function OrderTypeSelect({ types, value, disabled, onChange }) {
  return (
    <label className="otype-picker" htmlFor="otype-select">
      <span>Order type</span>
      <select id="otype-select" value={value} disabled={disabled} onChange={e => onChange(e.target.value)}>
        {types.map(([v, label]) => <option key={v} value={v}>{label}</option>)}
      </select>
    </label>
  );
}

const MARKS = [25, 50, 75, 100];

// A 0-100% slider with quick marks underneath. The marks keep data-pct so the
// buttons stay addressable; the slider reports on release as well as while dragging.
export function SizeSlider({ value, onChange, disabled, title }) {
  const pct = Math.max(0, Math.min(100, Number(value) || 0));
  return (
    <div className="size-slider" title={title}>
      <div className="size-slider-track">
        <input type="range" min="0" max="100" step="1" value={pct} disabled={disabled} aria-label="Size percent"
          style={{ "--fill": `${pct}%` }} onChange={e => onChange(Number(e.target.value))} />
        <span className="size-slider-value">{pct}%</span>
      </div>
      <div className="size-quick">
        {MARKS.map(p => (
          <button key={p} type="button" disabled={disabled} data-pct={p} className={pct === p ? "active" : ""}
            onClick={() => onChange(p)}>{p}%</button>
        ))}
      </div>
    </div>
  );
}
