/* Live status of the discipline rules derived from this account's own trade history.
   Display only: this component never places, cancels or modifies an order. */

import useStore from "../store";
import { RULES, evaluateRules, formatCountdown } from "../rules.js";
import { fmt } from "../api";

const PCT = value => (value === null || !Number.isFinite(value) ? "–" : `${(value * 100).toFixed(1)}%`);
const USD = value => (!Number.isFinite(value)
  ? "–" : `${value >= 0 ? "+$" : "-$"}${fmt(Math.abs(value), 2)}`);

function Bar({ progress, state }) {
  const filled = Math.max(0, Math.min(1, Number.isFinite(progress) ? progress : 0));
  return (
    <span className={"rule-bar rule-bar-" + state} aria-hidden="true">
      <span className="rule-bar-fill" style={{ width: `${filled * 100}%` }} />
    </span>
  );
}

export default function RulesPanel() {
  const positions = useStore(s => s.positions);
  const account = useStore(s => s.account);
  const instruments = useStore(s => s.instruments);
  const tickers = useStore(s => s.tickers);
  const rulePeaks = useStore(s => s.rulePeaks);
  const realized = useStore(s => s.realizedRecent);
  const statsState = useStore(s => s.statsState);
  const computeUpnl = useStore(s => s.computeUpnl);
  const positionsState = useStore(s => s.dataStatus.positions?.state);
  const accountState = useStore(s => s.dataStatus.account?.state);

  const stale = positionsState !== "current" || accountState !== "current";
  const result = evaluateRules({
    positions, account, instruments, tickers,
    peaks: rulePeaks,
    realized,
    upnlFor: computeUpnl,
    now: Date.now(),
  });

  const { rows, basket, cooldown, equity } = result;
  const oversized = rows.filter(r => r.size?.breach);

  return (
    <div className={"rules-panel" + (stale ? " rules-stale" : "")}>
      <div className="rules-head">
        <span className="k">Rules</span>
        {stale
          ? <span className="v muted" title="Positions or account data is not current; rules are not evaluated against live numbers">stale</span>
          : equity === null
            ? <span className="v muted" title="Equity unavailable">no equity</span>
            : <span className="v muted">{`TP $${fmt(equity * RULES.takeProfitPct, 0)}`}</span>}
      </div>

      {/* Rule 1: take profit at 3% of equity, per position. */}
      {!rows.length && <div className="acct-row"><span className="k">Take profit</span><span className="v muted">no positions</span></div>}
      {rows.map(row => {
        const tp = row.takeProfit;
        if (!tp) {
          return (
            <div className="acct-row" key={row.symbol}>
              <span className="k">{row.symbol.replace(/^PF_|^PI_/, "")}</span>
              <span className="v muted" title="Unrealized PnL unavailable for this position">–</span>
            </div>
          );
        }
        return (
          <div className="rule-row" key={row.symbol}>
            <div className="acct-row">
              <span className="k">{row.symbol.replace(/^PF_|^PI_/, "")}</span>
              <span className={"v " + (tp.upnl >= 0 ? "up" : "down")}>{PCT(tp.pct)}</span>
            </div>
            <Bar progress={tp.progress} state={tp.state} />
            <div className="rule-note">
              {tp.state === "take" && <b className="rule-fire">TAKE PROFIT · {USD(tp.upnl)}</b>}
              {tp.state === "trail" && <b className="rule-fire">GAVE BACK {PCT(tp.giveback)} · {USD(tp.upnl)}</b>}
              {tp.state === "hold" && (
                <span className="muted">
                  {USD(tp.upnl)} of {USD(tp.target)}
                  {tp.armed && tp.giveback !== null ? ` · back ${PCT(tp.giveback)}` : ""}
                </span>
              )}
            </div>
          </div>
        );
      })}

      {/* Rule 2: no single position above 3x equity notional. */}
      <div className="acct-row rules-sep">
        <span className="k">Size cap {RULES.sizeCapX}x</span>
        {!rows.length
          ? <span className="v muted">–</span>
          : oversized.length
            ? <span className="v down" title={oversized.map(r => `${r.symbol} ${r.size.multiple.toFixed(1)}x`).join(", ")}>
                {oversized.length} over
              </span>
            : <span className="v up">ok</span>}
      </div>
      {oversized.map(row => (
        <div className="rule-note rule-fire" key={row.symbol}>
          {row.symbol.replace(/^PF_|^PI_/, "")} {row.size.multiple.toFixed(1)}x · ${fmt(row.size.notional, 0)}
        </div>
      ))}

      {/* Rule 3: two-hour pause after a realized loss over 3% of equity. */}
      <div className="acct-row">
        <span className="k">Cooldown</span>
        {statsState === "unavailable"
          ? <span className="v muted" title="Realized-PnL ledger unavailable, so a recent loss cannot be ruled out">unknown</span>
          : statsState === "loading"
            ? <span className="v muted">…</span>
            : cooldown?.active
              ? <span className="v down">{formatCountdown(cooldown.remainingMs)} left</span>
              : <span className="v up">clear</span>}
      </div>
      {cooldown?.active && cooldown.trigger && (
        <div className="rule-note rule-fire">
          no new risk · {cooldown.trigger.contract.replace(/^PF_|^PI_/, "")} {USD(cooldown.trigger.net)}
        </div>
      )}

      {/* Basket exposure: warning only. The basket take-profit variant was tested
          and lost to the per-position rule at every threshold. */}
      <div className="acct-row rules-sep">
        <span className="k" title="Combined unrealized across all positions. Warning only — the take-profit rule is per position.">Basket</span>
        {basket.pct === null
          ? <span className="v muted">–</span>
          : <span className={"v " + (basket.warn ? "down" : basket.upnl >= 0 ? "up" : "")}>
              {PCT(basket.pct)}{basket.multiple !== null ? ` · ${basket.multiple.toFixed(1)}x` : ""}
            </span>}
      </div>
      {basket.warn && (
        <div className="rule-note rule-fire">
          book down {PCT(Math.abs(basket.pct))} of equity across {basket.positions}
        </div>
      )}
    </div>
  );
}
