import { useEffect, useRef, useState } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { fmt } from "../api";
import useStore from "../store";
import ThinkingTrace from "./ThinkingTrace";
import LoadingState from "./LoadingState";

const ESCAPED_DOLLAR = "\uE000";
const MARKDOWN_COMPONENTS = {
  table: ({ node: _node, ...props }) => <div className="md-table-wrap"><table {...props} /></div>,
};

function readableMath(text) {
  const plain = value => value
    .replace(/\\text\{([^{}]*)\}/g, "$1")
    .replace(/\\(?:mathbf|mathrm)\{([^{}]*)\}/g, "$1")
    .replace(/\\times/g, "×")
    .replace(/\\cdot/g, "·")
    .replace(/\\(?:left|right)/g, "")
    .replace(/\\,/g, " ")
    .replace(/[{}]/g, "")
    .trim();
  return String(text || "")
    .replace(/\\\$/g, ESCAPED_DOLLAR)
    .replace(/\$\$([\s\S]*?)\$\$/g, (_, value) => `\n\n**${plain(value)}**\n\n`)
    .replace(/\$([^$\n]+)\$/g, (_, value) => plain(value))
    .replaceAll(ESCAPED_DOLLAR, "$");
}

function MarkdownBody({ text }) {
  return <div className="md-body"><Markdown remarkPlugins={[remarkGfm]} components={MARKDOWN_COMPONENTS}>{readableMath(text)}</Markdown></div>;
}

function describeAction(a) {
  switch (a.type) {
    case "order": return `${a.side} ${fmt(a.size)} ${a.symbol} ${a.orderType}${a.limitPrice ? " @ " + fmt(a.limitPrice) : ""}${a.stopPrice ? " trg " + fmt(a.stopPrice) : ""}${a.reduceOnly ? " reduce-only" : ""}`;
    case "ladder": return `${a.side} grid $${fmt(a.notional)} × ${a.orders} orders over ${a.depthPercent}% (${a.orderType || "post"}) on ${a.symbol}`;
    case "chase": return `chase ${a.side} ${fmt(a.size)} ${a.symbol} post-only @ best ${a.side === "buy" ? "bid" : "ask"}${a.timeoutSec ? `, max ${a.timeoutSec}s` : ""}`;
    case "close": return `close ${a.percent != null ? a.percent + "%" : fmt(a.size) + " ctr"} of ${a.symbol}`;
    case "replace_tp": return `replace TP on ${a.symbol} → ${fmt(a.stopPrice)} (mark)`;
    case "replace_sl": return `replace SL on ${a.symbol} → ${fmt(a.stopPrice)} (mark)`;
    case "cancel_all": return `cancel all ${a.symbol}`;
    case "cancel": return `cancel ${String(a.cliOrdId || a.orderId || "").slice(0, 12)}`;
    default: return JSON.stringify(a).slice(0, 80);
  }
}

function ProposalCard({ acts, onExecute, disabled }) {
  return (
    <div className="proposal">
      <b>Trade actions ({acts.length})</b>
      {acts.map((a, i) => (
        <div className="p-row" key={i}>
          <span className="k">{a.type || "?"}</span>
          <span>{describeAction(a)}</span>
        </div>
      ))}
      <button disabled={disabled} onClick={() => onExecute(acts)}>{disabled ? "Executing…" : `Execute ${acts.length} action${acts.length > 1 ? "s" : ""} →`}</button>
    </div>
  );
}

export default function ChatPanel() {
  const chat = useStore(s => s.chat);
  const busy = useStore(s => s.chatBusy);
  const executeActions = useStore(s => s.executeActions);
  const actionBusy = useStore(s => s.actionBusy);
  const toggleRight = useStore(s => s.toggleRight);
  const sendChat = useStore(s => s.sendChat);
  const [input, setInput] = useState("");
  const boxRef = useRef(null);

  useEffect(() => {
    if (boxRef.current) boxRef.current.scrollTop = boxRef.current.scrollHeight;
  }, [chat]);

  const submit = () => {
    const text = input.trim();
    if (!text || busy) return;
    setInput("");
    sendChat(text);
  };

  return (
    <div className="panel" id="chat">
      <h3 className="chat-head">AI assistant
        <span className="chat-head-actions">
          <button className="chat-collapse" aria-label="Collapse order ticket and AI assistant" title="Collapse right panel" onClick={toggleRight}>›</button>
          <button className="chat-reset" title="Wipe conversation — memory file is kept" onClick={() => useStore.getState().resetChat()}>
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M3 12a9 9 0 1 0 3-6.7L3 8" /><path d="M3 3v5h5" />
            </svg>
          </button>
        </span>
      </h3>
      <div id="chat-messages" ref={boxRef}>
        {chat.map((m, i) => (
          m.role === "user"
            ? <div className="msg user" key={i}><div className="msg-role">You</div><div className="msg-copy">{m.content}</div></div>
            : m.kind === "trace" && m.trace
              ? <ThinkingTrace key={i} working={m.trace.working} rows={m.trace.rows} doneLabel={m.trace.doneLabel} defaultExpanded={false} />
              : (
                <div className="msg ai" key={i}>
                  <div className="msg-role">AI</div>
                  <MarkdownBody text={m.content} />
                  {(m.proposals || []).map((p, j) => (
                    <div className="proposal" key={"p" + j}>
                      <b>Order proposal</b>
                      <div className="p-row"><span className="k">Symbol</span><span>{p.symbol}</span></div>
                      <div className="p-row"><span className="k">Side</span><span className={p.side === "buy" ? "up" : "down"}>{p.side}</span></div>
                      <div className="p-row"><span className="k">Type</span><span>{p.orderType}</span></div>
                      <div className="p-row"><span className="k">Size</span><span>{fmt(p.size)}</span></div>
                      {p.limitPrice ? <div className="p-row"><span className="k">Limit</span><span>{fmt(p.limitPrice)}</span></div> : null}
                      {p.stopPrice ? <div className="p-row"><span className="k">Trigger</span><span>{fmt(p.stopPrice)}</span></div> : null}
                      <button onClick={() => useStore.getState().fillTicket(p)}>Fill order ticket →</button>
                    </div>
                  ))}
                  {(m.actionBlocks || []).map((acts, j) => (
                    <ProposalCard key={"a" + j} acts={acts} disabled={actionBusy} onExecute={(x) => executeActions(x, i, j)} />
                  ))}
                </div>
              )
        ))}
        {busy && <LoadingState label="Thinking" variant="Drive" />}
        <ChaseTraces />
      </div>
      <form id="chat-form" onSubmit={e => { e.preventDefault(); submit(); }}>
        <textarea
          id="chat-input"
          placeholder="Ask about the market, your positions… (Enter to send)"
          rows="1"
          value={input}
          onChange={e => setInput(e.target.value)}
          onKeyDown={e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); } }}
        ></textarea>
        <button id="chat-send" type="submit" disabled={busy}>Send</button>
      </form>
    </div>
  );
}

function ChaseTraces() {
  const chases = useStore(s => s.chases);
  const list = Object.values(chases || {})
    .sort((a, b) => (b.updated || 0) - (a.updated || 0))
    .slice(0, 3);
  if (!list.length) return null;
  return (
    <>
      {list.map(c => (
        <ThinkingTrace
          key={c.id}
          working={c.status === "running"}
          doneLabel={`Chase ${c.id}: ${c.status} — filled ${fmt(c.filled)}/${fmt(c.size)} ${c.symbol}`}
          rows={(c.events || []).map(e => ({ status: "ok", primary: e }))}
        />
      ))}
    </>
  );
}
