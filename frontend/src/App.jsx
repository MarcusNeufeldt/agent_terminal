import { useEffect } from "react";
import Sidebar from "./components/Sidebar";
import HeaderBar from "./components/HeaderBar";
import ChartPanel from "./components/ChartPanel";
import Ticket from "./components/Ticket";
import BottomTabs from "./components/BottomTabs";
import ChatPanel from "./components/ChatPanel";
import Toasts, { ToastHost } from "./components/Toasts";
import { bootTerminal } from "./components/boot";
import useStore from "./store";

export default function App() {
  const armed = useStore(s => s.armed);
  const pro = useStore(s => s.pro);
  const rightCollapsed = useStore(s => s.rightCollapsed);
  const toggleRight = useStore(s => s.toggleRight);

  useEffect(() => { bootTerminal(); }, []);
  useEffect(() => {
    document.title = armed ? "ARMED — Kraken Futures Terminal" : "Kraken Futures Terminal";
  }, [armed]);
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
        <ChartPanel source={useStore.getState().chartSource} />
        <BottomTabs />
      </main>
      <aside id="right" aria-label="Order ticket and AI assistant">
        <button
          className="right-expand"
          aria-label="Expand order ticket and AI assistant"
          aria-expanded={!rightCollapsed}
          title="Expand order ticket and AI assistant"
          onClick={toggleRight}
        >‹</button>
        <div className="right-inner">
          <Ticket />
          <ChatPanel />
        </div>
      </aside>
      <Toasts />
      <ToastHost />
    </>
  );
}
