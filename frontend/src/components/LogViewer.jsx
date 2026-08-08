import { useEffect, useRef } from "react";

// Scrollable, auto-scrolling log panel. `lines` is a list of strings.
export default function LogViewer({ lines = [], autoScroll = true }) {
  const endRef = useRef(null);

  useEffect(() => {
    if (autoScroll && endRef.current) {
      endRef.current.scrollIntoView({ behavior: "auto" });
    }
  }, [lines, autoScroll]);

  return (
    <div className="apex-scroll h-96 overflow-y-auto rounded-lg border border-gray-700 bg-black/40 p-4 font-mono text-xs leading-relaxed">
      {lines.length === 0 ? (
        <p className="text-gray-500">No log output yet.</p>
      ) : (
        lines.map((line, i) => (
          <div key={i} className="whitespace-pre-wrap break-all text-gray-300">
            {line}
          </div>
        ))
      )}
      <div ref={endRef} />
    </div>
  );
}
