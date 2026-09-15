import { useEffect } from "react";
import Sidebar from "./components/Sidebar";
import HeaderBar from "./components/HeaderBar";
import ChartPanel from "./components/ChartPanel";
import Ticket from "./components/Ticket";
import BottomTabs from "./components/BottomTabs";
import ChatPanel from "./components/ChatPanel";
import Toasts, { ToastHost } from "./components/Toasts";
import HyperliquidTicket from "./components/HyperliquidTicket";
import { bootTerminal } from "./components/boot";
import useStore from "./store";

export default function App() {
  const armed = useStore(s => s.armed);
  const exchangeName = useStore(s => s.exchangeName);
  const readOnly = useStore(s => s.readOnly);
  const symbol = useStore(s => s.symbol);
  const closeDraftId = useStore(s => s.hlCloseDraft?.id);
  const canTrade = useStore(s => s.canTrade);
  const pro = useStore(s => s.pro);
  const rightCollapsed = useStore(s => s.rightCollapsed);
  const toggleRight = useStore(s => s.toggleRight);
  const rightView = useStore(s => s.rightView);
  const showRight = useStore(s => s.showRight);
  const chatBusy = useStore(s => s.chatBusy);

  useEffect(() => {
    const controller = new AbortController();
    bootTerminal(controller.signal);
    return () => controller.abort();
  }, []);
  useEffect(() => {
    document.title = `${armed ? "ARMED · " : ""}${exchangeName} Terminal${!canTrade ? " · Read-only" : ""}`;
  }, [armed, exchangeName, canTrade]);
  useEffect(() => {
    document.body.classList.toggle("pro", pro);
  }, [pro]);
  useEffect(() => {
    document.body.classList.toggle("right-collapsed", rightCollapsed);
  }, [rightCollapsed]);

  return (
    <>
      <Sidebar />
      <HeaderBar />
      <main id="main">
        <ChartPanel />
        <BottomTabs />
      </main>
      <aside id="right" aria-label={readOnly ? "Hyperliquid read-only market view" : "Order ticket and AI assistant"}>
        <button
          className="right-expand"
          aria-label={readOnly ? "Expand market view" : "Expand order ticket and AI assistant"}
          aria-expanded={!rightCollapsed}
          title={readOnly ? "Expand market view" : "Expand order ticket and AI assistant"}
          onClick={toggleRight}
        >‹</button>
        <div className="right-inner">
          <div className="right-view-switch" role="group" aria-label="Sidebar view">
            <button aria-pressed={rightView === "ticket"} aria-controls="ticket-pane" onClick={() => showRight("ticket")}>{readOnly ? "Market view" : "Order ticket"}</button>
            <button disabled={readOnly} aria-pressed={rightView === "agent"} aria-controls="agent-pane" onClick={() => showRight("agent")}>AI assistant{chatBusy && <span className="agent-working"> · Working</span>}</button>
            <button className="right-hide" aria-label="Collapse sidebar" title="Collapse sidebar" onClick={toggleRight}>›</button>
          </div>
          <div id="ticket-pane" className="right-pane" hidden={rightView !== "ticket"}>
            {readOnly ? <>
              {!canTrade && <div className="exchange-notice" role="status">
                <h3>Hyperliquid · Read-only</h3>
                <p>Signed trading is off. Set <code>HYPERLIQUID_TRADING</code> to <code>mainnet</code> or <code>testnet</code>, add <code>HYPERLIQUID_SECRET_KEY</code>, then restart the backend.</p>
              </div>}
              <HyperliquidTicket key={`${symbol}:${closeDraftId || "regular"}`} />
            </> : <Ticket />}
          </div>
          <div id="agent-pane" className="right-pane" hidden={rightView !== "agent"}>{!readOnly && <ChatPanel />}</div>
        </div>
      </aside>
      <Toasts />
      <ToastHost />
    </>
  );
}
