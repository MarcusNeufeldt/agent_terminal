import { useEffect } from "react";
import useStore from "../store";

export default function Toasts() {
  const toasts = useStore(s => s.toasts);
  return (
    <div id="toasts">
      {toasts.map(t => (
        <div key={t.id} className={"toast " + t.kind} dangerouslySetInnerHTML={{ __html: t.msg }}></div>
      ))}
    </div>
  );
}

export function ToastHost() {
  const toast = useStore(s => s.toast);
  useEffect(() => {
    window.__toast = (msg, kind, ms) => toast(msg, kind, ms);
  }, [toast]);
  return null;
}
