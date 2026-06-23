import { useEffect, useRef, useState } from "react";

// Returns a transient flash class ("flash-up" / "flash-down" / "") whenever a
// numeric value changes between renders. Used to make live figures visibly tick.
export function useFlash(value, { numeric = true } = {}) {
  const prev = useRef(value);
  const [flash, setFlash] = useState("");

  useEffect(() => {
    const before = prev.current;
    prev.current = value;
    if (before === undefined || before === null || before === value) return undefined;

    let dir = "";
    if (numeric) {
      const a = Number(before);
      const b = Number(value);
      if (!Number.isNaN(a) && !Number.isNaN(b) && a !== b) {
        dir = b > a ? "flash-up" : "flash-down";
      }
    } else if (before !== value) {
      dir = "flash-up";
    }
    if (!dir) return undefined;

    setFlash(dir);
    const id = setTimeout(() => setFlash(""), 900);
    return () => clearTimeout(id);
  }, [value, numeric]);

  return flash;
}

// A figure that briefly flashes green/red when its value changes. `format`
// turns the raw value into display text; `className` styles the text.
export default function LiveValue({
  value,
  format = (v) => v,
  className = "",
  as: Tag = "span",
}) {
  const flash = useFlash(value);
  return (
    <Tag className={`num rounded-sm px-0.5 ${flash} ${className}`}>
      {format(value)}
    </Tag>
  );
}
